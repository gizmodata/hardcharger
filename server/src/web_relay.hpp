// The web UI's relay to other Hardcharger servers, and the UI's contract with the program
// that serves it. A browser can't talk to a second Chargewire server on its own (the
// protocol's CORS is an exact-origin allow-list, and a desktop workspace's origin is a
// random loopback port), so the program serving the UI forwards for it: the signed-in UI
// registers a remote (its URL and how to trust its certificate), then sends the remote's
// Chargewire requests to /ui/v1/remotes/<id>/chargewire/... on its own origin, with the
// remote's bearer token in Chargewire-Relay-Authorization, and the relay forwards them to
// the remote's /chargewire/... paths only, streaming request and response bodies both ways.
//
// Who may use the relay: a *relay session*, one code path whoever serves the UI. On a
// Hardcharger server the UI's own sign-in makes one (the request's bearer token, which the
// SDK validated; the session is keyed by that token). Where nothing local can be signed in
// to (`hardcharger ui`), registering a remote makes a pending session, which the UI keeps
// through the Chargewire-Relay-Session header, and that session may only reach the
// remote's discovery and sign-in endpoints until a sign-in to one of its remotes succeeds.
// Remotes live in their session; a session ends with its lifetime, unused.
//
// Engine-free: this and web_ui.cc make up the hardcharger_web library, which the server
// and the `hardcharger ui` command both link.
#pragma once

#include <chrono>
#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "chargewire/server.hpp"

namespace hardcharger::web {

using RelayLogFields = std::vector<std::pair<std::string, std::string>>;

struct RelayOptions {
  // The serving program is a Hardcharger server people sign in to (the UI's "This server").
  // false: `hardcharger ui`, where the UI opens on Add a server.
  bool local_server = true;
  // The relay is offered at all (the server's web_ui_remote_servers option). Off: the
  // remotes endpoints answer 404 saying so; the contract says remote_servers false.
  bool remote_servers = true;
  // The serving program's version, for the contract (GET /ui/v1/app).
  std::string version;
  std::size_t max_remotes_per_session = 16;
  std::size_t max_sessions = 4096;
  // A relay session that is used keeps living; one unused for this long is forgotten.
  std::chrono::seconds session_lifetime{std::chrono::hours(24)};
  // A pending session (no sign-in anywhere yet) is forgotten after this, unused.
  std::chrono::seconds pending_lifetime{600};
  // Time allowed to connect to a remote and to get its answer's headers, and between two
  // pieces of a streamed body.
  std::chrono::seconds connect_timeout{10};
  std::chrono::seconds response_timeout{600};
  std::chrono::seconds body_timeout{300};
  // Where to log (a warning or not, the message, fields); unset: nothing is logged.
  std::function<void(bool warning, const std::string& message, const RelayLogFields& fields)> log;
};

class Relay {
 public:
  explicit Relay(RelayOptions options);
  ~Relay();
  Relay(const Relay&) = delete;
  Relay& operator=(const Relay&) = delete;

  // The endpoints to install (chargewire::ServerOptions::app_endpoints): the contract at
  // /ui/v1/app, the remotes API at /ui/v1/remotes and the forwarding path beneath it. The
  // Relay must outlive the server they are installed on.
  std::vector<chargewire::AppEndpoint> Endpoints();

  // Live relay sessions (for tests).
  std::size_t SessionCount() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

// A certificate's SHA-256 fingerprint as the relay shows and compares it: lowercase hex
// pairs joined by ':'. Accepts the same with any case, with or without colons or spaces;
// empty when `text` is not a fingerprint.
std::string NormalizeFingerprint(std::string_view text);

}  // namespace hardcharger::web
