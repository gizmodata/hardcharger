// The web UI (gizmodata/hardcharger-ui): a single-page app embedded in the binary at build
// time (server/cmake/web_ui.cmake) and served at /ui on the server's own listener when
// web_ui is on. The rules are hardcharger-ui's serving contract (its scripts/ui-contract.mjs):
// files under /ui/, index.html for the app's own routes, hashed assets cached for good,
// everything else revalidated, and a strict Content-Security-Policy on every answer.
#pragma once

#include <cstddef>
#include <optional>
#include <span>
#include <string_view>

#include "warpsql/server.hpp"

namespace hardcharger {

struct WebUiFile {
  std::string_view path;  // relative to /ui/, e.g. "index.html", "assets/index-1a2b3c4d.js"
  const unsigned char* data;
  std::size_t size;
};

// The files this build embeds (none without -DHARDCHARGER_WEB_UI=<tarball>).
std::span<const WebUiFile> EmbeddedWebUiFiles();
// The embedded UI's version ("" without one).
std::string_view EmbeddedWebUiVersion();

// The Content-Security-Policy every /ui answer carries.
extern const std::string_view kWebUiContentSecurityPolicy;

// The answer to a GET or HEAD of `path` (percent-decoded) from `files`, or nullopt when the
// path is not the UI's (not /ui or under /ui/).
std::optional<warpsql::PlainResponse> ServeWebUi(std::span<const WebUiFile> files, std::string_view path);

}  // namespace hardcharger
