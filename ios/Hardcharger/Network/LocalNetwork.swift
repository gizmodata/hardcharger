import Foundation

/// The device's addresses on its local networks: where a laptop on the same Wi-Fi reaches
/// the server when it is shared.
enum LocalNetwork {
    struct Address: Sendable, Hashable, Identifiable {
        let interface: String
        let ip: String
        var id: String { "\(interface) \(ip)" }
        /// Wi-Fi (en0) or a wired adapter, which a person names differently.
        var isWiFi: Bool { interface == "en0" }
    }

    /// IPv4 addresses on Wi-Fi and wired interfaces, up: not loopback, link-local (169.254),
    /// cellular (pdp_ip) or VPN tunnels (utun, ipsec), which other devices can't reach.
    static func addresses() -> [Address] {
        var result: [Address] = []
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return [] }
        defer { freeifaddrs(list) }
        for pointer in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let entry = pointer.pointee
            guard let address = entry.ifa_addr, address.pointee.sa_family == UInt8(AF_INET) else { continue }
            let flags = Int32(entry.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            let name = String(cString: entry.ifa_name)
            guard name.hasPrefix("en") || name.hasPrefix("bridge") else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0,
                              NI_NUMERICHOST) == 0
            else { continue }
            let ip = String(cBuffer: host)
            if ip.hasPrefix("169.254.") { continue }
            result.append(Address(interface: name, ip: ip))
        }
        return result.sorted { ($0.isWiFi ? 0 : 1, $0.interface, $0.ip) < ($1.isWiFi ? 0 : 1, $1.interface, $1.ip) }
    }
}
