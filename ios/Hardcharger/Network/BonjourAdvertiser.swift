import Foundation

/// Advertises the shared server on the local network as `_hardcharger._tcp` (Bonjour), for
/// tools that look for it; on a device, advertising is also what asks the person for local
/// network access, which the server needs before other devices can reach it.
@MainActor
final class BonjourAdvertiser {
    static let serviceType = "_hardcharger._tcp."
    private var service: NetService?

    func advertise(port: Int, name: String) {
        if let service, service.port == port { return }
        stop()
        let service = NetService(domain: "local.", type: Self.serviceType, name: name, port: Int32(port))
        service.setTXTRecord(NetService.data(fromTXTRecord: [
            "path": Data("/chargewire".utf8), "tls": Data("1".utf8), "ui": Data("/ui/".utf8),
        ]))
        service.publish()
        self.service = service
    }

    func stop() {
        service?.stop()
        service = nil
    }
}
