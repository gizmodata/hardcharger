import CryptoKit
import Foundation
import Security

/// The on-device server's TLS certificate for Share on Local Network: self-signed, made on
/// the device with a P-256 key (CryptoKit), naming the device's addresses. Browsers warn
/// about a certificate no authority signed; the person compares the SHA-256 fingerprint the
/// app shows with the browser's. The app's own web view and clients trust exactly this
/// certificate (by fingerprint), nothing else self-signed.
struct SelfSignedCertificate: Sendable, Equatable {
    /// The certificate, DER.
    let der: Data
    /// PEM files for the server (tls_cert, tls_key).
    let certificatePEM: String
    let privateKeyPEM: String
    let notAfter: Date
    /// DNS names and IP addresses it names.
    let dnsNames: [String]
    let ipAddresses: [String]

    /// SHA-256 of the DER certificate, as browsers show it: colon-separated uppercase hex.
    var fingerprint: String { Self.fingerprint(of: der) }

    static func fingerprint(of der: Data) -> String {
        SHA256.hash(data: der).map { String(format: "%02X", $0) }.joined(separator: ":")
    }

    /// Browsers refuse server certificates valid for more than 398 days.
    static let validity: TimeInterval = 397 * 24 * 3600

    static func make(commonName: String, dnsNames: [String], ipAddresses: [String], now: Date = .now) throws
        -> SelfSignedCertificate
    {
        let key = P256.Signing.PrivateKey()
        let notBefore = now.addingTimeInterval(-3600)  // a little slack for clocks behind
        let notAfter = now.addingTimeInterval(validity)
        var serial = [UInt8](repeating: 0, count: 16)
        _ = SecRandomCopyBytes(kSecRandomDefault, serial.count, &serial)
        serial[0] &= 0x7F  // positive

        let ecdsaWithSHA256 = DER.sequence([DER.oid([1, 2, 840, 10045, 4, 3, 2])])
        let name = DER.sequence([
            DER.set([DER.sequence([DER.oid([2, 5, 4, 3]), DER.utf8String(commonName)])]),
            DER.set([DER.sequence([DER.oid([2, 5, 4, 10]), DER.utf8String("Hardcharger")])]),
        ])
        var alternativeNames: [Data] = dnsNames.map { DER.contextPrimitive(2, Data($0.utf8)) }
        for ip in ipAddresses {
            if let bytes = IPAddress.bytes(ip) { alternativeNames.append(DER.contextPrimitive(7, bytes)) }
        }
        let extensions = DER.contextConstructed(3, [
            DER.sequence([
                // Subject alternative names
                DER.sequence([DER.oid([2, 5, 29, 17]), DER.octetString(DER.sequence(alternativeNames))]),
                // Basic constraints: not a CA (critical)
                DER.sequence([DER.oid([2, 5, 29, 19]), DER.boolean(true), DER.octetString(DER.sequence([]))]),
                // Key usage: digital signature (critical)
                DER.sequence([
                    DER.oid([2, 5, 29, 15]), DER.boolean(true), DER.octetString(DER.bitString([0x80], unusedBits: 7)),
                ]),
                // Extended key usage: TLS server
                DER.sequence([
                    DER.oid([2, 5, 29, 37]), DER.octetString(DER.sequence([DER.oid([1, 3, 6, 1, 5, 5, 7, 3, 1])])),
                ]),
            ]),
        ])
        let tbs = DER.sequence([
            DER.contextConstructed(0, [DER.integer(2)]),  // v3
            DER.integer(bytes: serial),
            ecdsaWithSHA256,
            name,
            DER.sequence([DER.time(notBefore), DER.time(notAfter)]),
            name,
            key.publicKey.derRepresentation,  // SubjectPublicKeyInfo
            extensions,
        ])
        let signature = try key.signature(for: tbs).derRepresentation
        let certificate = DER.sequence([tbs, ecdsaWithSHA256, DER.bitString(signature)])
        return SelfSignedCertificate(
            der: certificate,
            certificatePEM: pem(certificate, label: "CERTIFICATE"),
            privateKeyPEM: key.pemRepresentation,
            notAfter: notAfter,
            dnsNames: dnsNames,
            ipAddresses: ipAddresses)
    }

    static func pem(_ der: Data, label: String) -> String {
        let base64 = der.base64EncodedString(options: [.lineLength64Characters, .endLineWithLineFeed])
        return "-----BEGIN \(label)-----\n\(base64)\n-----END \(label)-----\n"
    }

    /// The DER inside a PEM certificate file.
    static func der(fromPEM pem: String) -> Data? {
        let body = pem.components(separatedBy: "\n")
            .filter { !$0.hasPrefix("-----") && !$0.isEmpty }
            .joined()
        return Data(base64Encoded: body)
    }
}

/// The server's certificate and key files: made when missing, and made again when they no
/// longer name every address the device has (it joined another network) or expire within
/// 30 days. `tls-certificate.json` beside them records what the certificate names.
enum ServerCertificateFiles {
    struct Current: Sendable, Equatable {
        let der: Data
        let notAfter: Date
        let ipAddresses: [String]
        var fingerprint: String { SelfSignedCertificate.fingerprint(of: der) }
    }

    private struct Metadata: Codable {
        let notAfter: Date
        let ipAddresses: [String]
    }

    static func ensure(paths: AppPaths, addresses: [String], hostName: String, now: Date = .now) throws -> Current {
        if let existing = load(paths: paths),
           existing.notAfter.timeIntervalSince(now) > 30 * 24 * 3600,
           Set(addresses).isSubset(of: Set(existing.ipAddresses)) {
            return existing
        }
        return try regenerate(paths: paths, addresses: addresses, hostName: hostName, now: now)
    }

    @discardableResult
    static func regenerate(paths: AppPaths, addresses: [String], hostName: String, now: Date = .now) throws -> Current {
        try paths.createDirectories()
        var dns = ["localhost"]
        if !hostName.isEmpty, !dns.contains(hostName) { dns.append(hostName) }
        let ips = Array(Set(["127.0.0.1", "::1"] + addresses)).sorted()
        let certificate = try SelfSignedCertificate.make(
            commonName: hostName.isEmpty ? "Hardcharger" : hostName, dnsNames: dns, ipAddresses: ips, now: now)
        // The key is readable once the device has been unlocked after it started.
        try Data(certificate.privateKeyPEM.utf8).write(
            to: paths.tlsKey, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        try Data(certificate.certificatePEM.utf8).write(to: paths.tlsCertificate, options: .atomic)
        let metadata = Metadata(notAfter: certificate.notAfter, ipAddresses: ips)
        try encoder.encode(metadata).write(to: metadataFile(paths), options: .atomic)
        return Current(der: certificate.der, notAfter: certificate.notAfter, ipAddresses: ips)
    }

    static func load(paths: AppPaths) -> Current? {
        guard let pem = try? String(contentsOf: paths.tlsCertificate, encoding: .utf8),
              FileManager.default.fileExists(atPath: paths.tlsKey.path(percentEncoded: false)),
              let der = SelfSignedCertificate.der(fromPEM: pem),
              SecCertificateCreateWithData(nil, der as CFData) != nil,
              let data = try? Data(contentsOf: metadataFile(paths)),
              let metadata = try? decoder.decode(Metadata.self, from: data)
        else { return nil }
        return Current(der: der, notAfter: metadata.notAfter, ipAddresses: metadata.ipAddresses)
    }

    private static func metadataFile(_ paths: AppPaths) -> URL { paths.support.appending(path: "tls-certificate.json") }

    private static var encoder: JSONEncoder {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }

    private static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }
}

/// Minimal DER encoding: what a certificate needs.
enum DER {
    static func tlv(_ tag: UInt8, _ content: Data) -> Data {
        var out = Data([tag])
        out.append(length(content.count))
        out.append(content)
        return out
    }

    static func length(_ n: Int) -> Data {
        if n < 0x80 { return Data([UInt8(n)]) }
        var bytes: [UInt8] = []
        var value = n
        while value > 0 {
            bytes.insert(UInt8(value & 0xFF), at: 0)
            value >>= 8
        }
        return Data([0x80 | UInt8(bytes.count)] + bytes)
    }

    static func sequence(_ items: [Data]) -> Data { tlv(0x30, items.reduce(Data(), +)) }
    static func set(_ items: [Data]) -> Data { tlv(0x31, items.reduce(Data(), +)) }
    static func contextConstructed(_ n: UInt8, _ items: [Data]) -> Data { tlv(0xA0 | n, items.reduce(Data(), +)) }
    static func contextPrimitive(_ n: UInt8, _ content: Data) -> Data { tlv(0x80 | n, content) }
    static func boolean(_ value: Bool) -> Data { tlv(0x01, Data([value ? 0xFF : 0x00])) }
    static func octetString(_ content: Data) -> Data { tlv(0x04, content) }
    static func utf8String(_ text: String) -> Data { tlv(0x0C, Data(text.utf8)) }

    static func bitString(_ content: Data, unusedBits: UInt8 = 0) -> Data {
        var body = Data([unusedBits])
        body.append(content)
        return tlv(0x03, body)
    }

    static func bitString(_ bytes: [UInt8], unusedBits: UInt8) -> Data { bitString(Data(bytes), unusedBits: unusedBits) }

    static func integer(_ value: Int) -> Data { integer(bytes: [UInt8(value)]) }

    static func integer(bytes: [UInt8]) -> Data {
        var bytes = Array(bytes.drop(while: { $0 == 0 }))
        if bytes.isEmpty { bytes = [0] }
        if bytes[0] & 0x80 != 0 { bytes.insert(0, at: 0) }
        return tlv(0x02, Data(bytes))
    }

    static func oid(_ components: [UInt]) -> Data {
        var body = Data([UInt8(components[0] * 40 + components[1])])
        for component in components.dropFirst(2) {
            var chunk: [UInt8] = [UInt8(component & 0x7F)]
            var value = component >> 7
            while value > 0 {
                chunk.insert(UInt8(value & 0x7F) | 0x80, at: 0)
                value >>= 7
            }
            body.append(contentsOf: chunk)
        }
        return tlv(0x06, body)
    }

    /// UTCTime until 2049, GeneralizedTime after (RFC 5280 §4.1.2.5).
    static func time(_ date: Date) -> Data {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        let year = Calendar(identifier: .gregorian).dateComponents(in: TimeZone(identifier: "UTC")!, from: date).year ?? 2026
        if year < 2050 {
            formatter.dateFormat = "yyMMddHHmmss'Z'"
            return tlv(0x17, Data(formatter.string(from: date).utf8))
        }
        formatter.dateFormat = "yyyyMMddHHmmss'Z'"
        return tlv(0x18, Data(formatter.string(from: date).utf8))
    }
}

enum IPAddress {
    /// The 4 or 16 bytes of an IPv4 or IPv6 address in text form.
    static func bytes(_ text: String) -> Data? {
        var v4 = in_addr()
        if inet_pton(AF_INET, text, &v4) == 1 { return withUnsafeBytes(of: &v4) { Data($0) } }
        var v6 = in6_addr()
        if inet_pton(AF_INET6, text, &v6) == 1 { return withUnsafeBytes(of: &v6) { Data($0) } }
        return nil
    }
}
