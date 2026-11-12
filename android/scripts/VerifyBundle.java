import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.cert.CertificateFactory;
import java.util.Arrays;
import java.util.Collections;
import java.util.jar.JarFile;

// Verify every payload entry, including its digest and the intended upload certificate.
class VerifyBundle {
    public static void main(String[] args) throws Exception {
        java.security.cert.Certificate expected;
        try (var input = Files.newInputStream(Path.of(args[1]))) {
            expected = CertificateFactory.getInstance("X.509").generateCertificate(input);
        }
        int count = 0;
        try (var jar = new JarFile(args[0], true)) {
            for (var entry : Collections.list(jar.entries())) {
                if (entry.isDirectory()) continue;
                try (var input = jar.getInputStream(entry)) {
                    input.transferTo(OutputStream.nullOutputStream());
                }
                String name = entry.getName();
                if (name.equals("META-INF/MANIFEST.MF") || name.matches("META-INF/[^/]+\\.(SF|RSA|DSA|EC)")) continue;
                var certificates = entry.getCertificates();
                if (certificates == null || !Arrays.asList(certificates).contains(expected)) {
                    throw new SecurityException("Unexpected or missing signer: " + name);
                }
                count++;
            }
        }
        if (count == 0) throw new SecurityException("Empty bundle");
        System.out.println("Verified payload digests and expected upload certificate: " + count + " entries");
    }
}
