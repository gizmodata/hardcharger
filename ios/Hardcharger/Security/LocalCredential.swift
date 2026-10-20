import Foundation
import Security

/// The on-device server's administrator credential and token-signing key, made on first
/// launch and kept in the Keychain. Never in the app's code, its files or its logs: the
/// server receives them through its environment at start (`ServerLaunch`).
struct LocalCredential: Codable, Equatable, Sendable {
    var username: String
    var password: String
    /// Signs the server's access tokens, so a token outlives a restart of the server.
    var secretKey: String

    static let service = "com.gizmodata.hardcharger.local-server"
    static let account = "administrator"
    static let defaultUsername = "hardcharger"

    /// The password alphabet: letters and digits only. No ':' '@' '/' ';' or whitespace,
    /// which some clients split credentials on, and nothing a URL or a shell would mangle.
    static let passwordAlphabet = Array("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")
    static let passwordLength = 24

    /// The stored credential, or a new one (stored) if there is none.
    static func loadOrCreate() throws -> LocalCredential {
        if let data = try Keychain.read(service: service, account: account),
           let credential = try? JSONDecoder().decode(LocalCredential.self, from: data) {
            return credential
        }
        let credential = LocalCredential(
            username: defaultUsername, password: try randomPassword(), secretKey: try randomHex(bytes: 32))
        try credential.store()
        return credential
    }

    func store() throws {
        try Keychain.write(
            try JSONEncoder().encode(self), service: Self.service, account: Self.account,
            label: "Hardcharger on this device")
    }

    /// A new password (the key stays, so the web console's token keeps working until the
    /// server restarts with the new password).
    func withNewPassword() throws -> LocalCredential {
        var copy = self
        copy.password = try Self.randomPassword()
        return copy
    }

    static func randomPassword() throws -> String {
        let alphabet = passwordAlphabet
        // Rejection sampling over random bytes, so every character is equally likely.
        let limit = 256 - (256 % alphabet.count)
        var result = ""
        while result.count < passwordLength {
            for byte in try randomBytes(64) where Int(byte) < limit && result.count < passwordLength {
                result.append(alphabet[Int(byte) % alphabet.count])
            }
        }
        return result
    }

    static func randomHex(bytes: Int) throws -> String {
        try randomBytes(bytes).map { String(format: "%02x", $0) }.joined()
    }

    private static func randomBytes(_ count: Int) throws -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count)
        let status = SecRandomCopyBytes(kSecRandomDefault, count, &bytes)
        guard status == errSecSuccess else { throw Keychain.Failure(status: status, operation: "random") }
        return bytes
    }
}
