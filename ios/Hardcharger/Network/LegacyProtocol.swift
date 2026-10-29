import Foundation

/// The protocol's pre-rename spelling, for servers released before it: a server with no
/// discovery document at `/chargewire` but one at this root speaks the old spelling, and the
/// app talks to it there. The app sends none of the protocol's own headers, so the root is
/// all that differs. This file is the only place the app writes that spelling down.
enum LegacyProtocol {
    static let root = "warp"
    static let name = "warp-sql"
}
