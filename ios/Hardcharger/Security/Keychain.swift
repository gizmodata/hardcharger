import Foundation
import Security

/// Generic-password items in the app's Keychain: the on-device server's credential and the
/// saved remote connections. Readable after the first unlock, on this device only (never
/// in a backup restored elsewhere).
enum Keychain {
    struct Failure: Error, CustomStringConvertible {
        let status: OSStatus
        let operation: String
        var description: String {
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "status \(status)"
            return "Keychain \(operation): \(text)"
        }
    }

    static func read(service: String, account: String) throws -> Data? {
        var query = baseQuery(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw Failure(status: status, operation: "read") }
        return item as? Data
    }

    static func write(_ data: Data, service: String, account: String, label: String? = nil) throws {
        let query = baseQuery(service: service, account: account)
        var attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        if let label { attributes[kSecAttrLabel as String] = label }
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add.merge(attributes) { _, new in new }
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw Failure(status: status, operation: "write") }
    }

    static func delete(service: String, account: String) throws {
        let status = SecItemDelete(baseQuery(service: service, account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw Failure(status: status, operation: "delete")
        }
    }

    /// Every account of a service, with its data.
    static func all(service: String) throws -> [(account: String, data: Data)] {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecReturnAttributes as String: true,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitAll,
        ]
        var items: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &items)
        if status == errSecItemNotFound { return [] }
        guard status == errSecSuccess, let list = items as? [[String: Any]] else {
            throw Failure(status: status, operation: "list")
        }
        return list.compactMap { item in
            guard let account = item[kSecAttrAccount as String] as? String,
                  let data = item[kSecValueData as String] as? Data
            else { return nil }
            return (account, data)
        }
    }

    private static func baseQuery(service: String, account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}
