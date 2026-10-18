// The web UI (ui/ in this repository): a single-page app embedded in the binary at build time
// (server/cmake/web_ui.cmake, gzipped) and served at /ui on the server's own listener when
// web_ui is on. The rules are the UI's serving contract (ui/scripts/ui-contract.mjs): files
// under /ui/, index.html for the app's own routes, hashed assets cached for good, everything
// else revalidated, and a strict Content-Security-Policy on every answer. A client that
// accepts gzip gets the embedded bytes as they are (Content-Encoding: gzip); any other gets
// them inflated.
#pragma once

#include <cstddef>
#include <optional>
#include <span>
#include <string_view>

#include "warpsql/server.hpp"

namespace hardcharger {

struct WebUiFile {
  std::string_view path;  // relative to /ui/, e.g. "index.html", "assets/index-1a2b3c4d.js"
  const unsigned char* data;  // gzip when `gzipped`, else the file itself
  std::size_t size;           // of `data`
  std::size_t raw_size;       // of the file itself
  bool gzipped;
  std::string_view sha256;  // of the file itself (hex): its ETag
};

// The files this build embeds (none without -DHARDCHARGER_WEB_UI=<tarball>).
std::span<const WebUiFile> EmbeddedWebUiFiles();
// The embedded UI's version ("" without one).
std::string_view EmbeddedWebUiVersion();

// The Content-Security-Policy every /ui answer carries.
extern const std::string_view kWebUiContentSecurityPolicy;

// The answer to a GET or HEAD from `files`, or nullopt when the path is not the UI's (not
// /ui or under /ui/). Uses the request's Accept-Encoding and If-None-Match.
std::optional<warpsql::PlainResponse> ServeWebUi(std::span<const WebUiFile> files,
                                                 const warpsql::PlainRequest& request);

// Whether an Accept-Encoding value accepts gzip (a q of 0 refuses it, `*` stands for it).
bool AcceptsGzip(std::string_view accept_encoding);

}  // namespace hardcharger
