use crate::server::{private_write, random_secret, NetworkSettings, Settings};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    net::IpAddr,
    path::{Path, PathBuf},
};

#[derive(Serialize)]
pub struct Address {
    pub address: String,
    pub interface: String,
}

#[derive(Serialize)]
pub struct NetworkInfo {
    pub hostname: String,
    pub addresses: Vec<Address>,
}

pub fn info() -> Result<NetworkInfo, String> {
    let mut addresses: Vec<_> = if_addrs::get_if_addrs()
        .map_err(|e| e.to_string())?
        .into_iter()
        .filter(|i| {
            i.is_oper_up()
                && !i.is_p2p
                && match i.ip() {
                    IpAddr::V4(ip) => {
                        !ip.is_loopback() && !ip.is_unspecified() && !ip.is_link_local()
                    }
                    IpAddr::V6(ip) => {
                        !ip.is_loopback()
                            && !ip.is_unspecified()
                            && ip.segments()[0] & 0xffc0 != 0xfe80
                    }
                }
        })
        .map(|i| Address {
            address: i.ip().to_string(),
            interface: i.name,
        })
        .collect();
    addresses.sort_by_key(|i| {
        (
            i.address.contains(':'),
            i.interface != "en0",
            i.interface.clone(),
            i.address.clone(),
        )
    });
    addresses.dedup_by(|a, b| a.address == b.address);
    Ok(NetworkInfo {
        hostname: hostname::get()
            .map_err(|e| e.to_string())?
            .to_string_lossy()
            .into_owned(),
        addresses,
    })
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GeneratedCertificate {
    pub certificate: String,
    pub private_key: String,
    pub trust_certificate: String,
    pub fingerprint: String,
    pub expires: String,
    pub names: Vec<String>,
}

fn host_name(name: &str) -> Result<String, String> {
    let settings = Settings {
        network: NetworkSettings {
            enabled: true,
            server_name: name.trim().into(),
            tls: false,
            password: "address-validation".into(),
            ..NetworkSettings::default()
        },
        ..Settings::default()
    };
    settings.validate()?;
    let name = name.trim().trim_matches(['[', ']']);
    let authority = if name.contains(':') {
        format!("[{name}]")
    } else {
        name.into()
    };
    let url = tauri::Url::parse(&format!("https://{authority}")).map_err(|e| e.to_string())?;
    Ok(url
        .host_str()
        .ok_or("Missing server address.")?
        .trim_matches(['[', ']'])
        .into())
}

pub fn generate(directory: &Path, server_name: &str) -> Result<GeneratedCertificate, String> {
    let mut names = vec![
        host_name(server_name)?,
        "localhost".into(),
        "127.0.0.1".into(),
        "::1".into(),
    ];
    let detected = info()?;
    if let Ok(host) = host_name(&detected.hostname) {
        names.push(host);
    }
    names.extend(detected.addresses.into_iter().map(|a| a.address));
    names.sort();
    names.dedup();
    let now = time::OffsetDateTime::now_utc();
    let expires = now + time::Duration::days(365);
    let mut ca_params = CertificateParams::default();
    ca_params.not_before = now - time::Duration::minutes(5);
    ca_params.not_after = expires;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    ca_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    ca_params.distinguished_name.push(
        DnType::CommonName,
        format!("Hardcharger ({server_name}) local CA"),
    );
    let ca_key = KeyPair::generate().map_err(|e| e.to_string())?;
    let ca = ca_params.self_signed(&ca_key).map_err(|e| e.to_string())?;
    let issuer = Issuer::from_params(&ca_params, &ca_key);
    let key = KeyPair::generate().map_err(|e| e.to_string())?;
    let mut params = CertificateParams::new(names.clone()).map_err(|e| e.to_string())?;
    params.not_before = now - time::Duration::minutes(5);
    params.not_after = expires;
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    params
        .distinguished_name
        .push(DnType::CommonName, server_name);
    let certificate = params.signed_by(&key, &issuer).map_err(|e| e.to_string())?;
    let folder = directory.join("certificates").join(random_secret()?);
    fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&folder, fs::Permissions::from_mode(0o700))
            .map_err(|e| e.to_string())?;
    }
    let cert_path = folder.join("server.pem");
    let key_path = folder.join("server.key");
    let trust_path = folder.join("Hardcharger-local-CA.crt");
    private_write(&key_path, key.serialize_pem().as_bytes())?;
    private_write(
        &cert_path,
        format!("{}{}", certificate.pem(), ca.pem()).as_bytes(),
    )?;
    private_write(&trust_path, ca.pem().as_bytes())?;
    // The CA private key is deliberately not written to disk or returned to the UI.
    let result = GeneratedCertificate {
        certificate: cert_path.to_string_lossy().into(),
        private_key: key_path.to_string_lossy().into(),
        trust_certificate: trust_path.to_string_lossy().into(),
        fingerprint: Sha256::digest(ca.der())
            .iter()
            .map(|b| format!("{b:02X}"))
            .collect::<Vec<_>>()
            .join(":"),
        expires: expires.date().to_string(),
        names,
    };
    private_write(
        &folder.join("certificate-info.json"),
        &serde_json::to_vec_pretty(&result).map_err(|e| e.to_string())?,
    )?;
    Ok(result)
}

pub fn generated(directory: &Path, certificate: &str) -> Option<GeneratedCertificate> {
    let path = Path::new(certificate);
    let folder = path.parent()?;
    if path.file_name()? != "server.pem" || folder.parent()? != directory.join("certificates") {
        return None;
    }
    let record: GeneratedCertificate =
        serde_json::from_slice(&fs::read(folder.join("certificate-info.json")).ok()?).ok()?;
    if record.certificate != certificate
        || Path::new(&record.trust_certificate) != folder.join("Hardcharger-local-CA.crt")
    {
        return None;
    }
    Some(record)
}

pub fn certificate_folder(directory: &Path, certificate: &str) -> Result<PathBuf, String> {
    let record = generated(directory, certificate)
        .ok_or("Choose a certificate generated by this app first.")?;
    Ok(Path::new(&record.trust_certificate)
        .parent()
        .ok_or("Missing certificate folder.")?
        .into())
}
