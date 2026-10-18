#include "web_ui.hpp"

#include <algorithm>
#include <string>
#include <utility>

namespace hardcharger {

// Everything the UI loads is a file of its own under /ui/. The exceptions, each needed
// (hardcharger-ui's e2e tests run under this policy and fail on any violation):
// 'wasm-unsafe-eval' for the Parquet export's WebAssembly (not JavaScript eval), inline
// styles for the editor, and data: images in the editor's stylesheet.
const std::string_view kWebUiContentSecurityPolicy =
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

namespace {

std::string_view ContentType(std::string_view path) {
  static constexpr std::pair<std::string_view, std::string_view> kTypes[] = {
      {".html", "text/html; charset=utf-8"},  {".js", "text/javascript; charset=utf-8"},
      {".mjs", "text/javascript; charset=utf-8"}, {".css", "text/css; charset=utf-8"},
      {".json", "application/json"},          {".wasm", "application/wasm"},
      {".png", "image/png"},                  {".svg", "image/svg+xml"},
      {".ico", "image/x-icon"},               {".ttf", "font/ttf"},
      {".woff", "font/woff"},                 {".woff2", "font/woff2"},
      {".txt", "text/plain; charset=utf-8"},
  };
  const auto slash = path.rfind('/');
  const auto dot = path.rfind('.');
  if (dot != std::string_view::npos && (slash == std::string_view::npos || dot > slash)) {
    const std::string_view ext = path.substr(dot);
    for (const auto& [suffix, type] : kTypes) {
      if (ext == suffix) return type;
    }
  }
  return "application/octet-stream";
}

warpsql::PlainResponse WithHeaders(warpsql::PlainResponse response) {
  response.headers.emplace_back("Content-Security-Policy", std::string(kWebUiContentSecurityPolicy));
  response.headers.emplace_back("X-Content-Type-Options", "nosniff");
  response.headers.emplace_back("Referrer-Policy", "no-referrer");
  response.headers.emplace_back("X-Frame-Options", "DENY");
  response.headers.emplace_back("Cross-Origin-Opener-Policy", "same-origin");
  response.headers.emplace_back("Cross-Origin-Resource-Policy", "same-origin");
  return response;
}

warpsql::PlainResponse NotFound() {
  warpsql::PlainResponse response;
  response.status = 404;
  response.content_type = "text/plain; charset=utf-8";
  // Never cached: another replica, or the next build, may have the file.
  response.headers.emplace_back("Cache-Control", "no-store");
  response.body = "not found\n";
  return WithHeaders(std::move(response));
}

const WebUiFile* Find(std::span<const WebUiFile> files, std::string_view path) {
  const auto it = std::ranges::find(files, path, &WebUiFile::path);
  return it == files.end() ? nullptr : &*it;
}

}  // namespace

std::optional<warpsql::PlainResponse> ServeWebUi(std::span<const WebUiFile> files, std::string_view path) {
  if (path == "/ui") {
    warpsql::PlainResponse response;
    response.status = 308;
    response.headers.emplace_back("Location", "/ui/");
    response.headers.emplace_back("Cache-Control", "no-cache");
    return WithHeaders(std::move(response));
  }
  if (!path.starts_with("/ui/")) return std::nullopt;
  std::string_view rest = path.substr(4);

  // No "." or ".." segments (the path is already percent-decoded), and no empty ones.
  for (std::size_t start = 0; start <= rest.size();) {
    const auto end = std::min(rest.find('/', start), rest.size());
    const std::string_view segment = rest.substr(start, end - start);
    if (segment == "." || segment == ".." || (segment.empty() && end < rest.size())) return NotFound();
    start = end + 1;
  }

  // A file; else, under assets/ or with a dot in its last segment, nothing (a missing
  // hashed file is a stale page, not a route); else the app's own route: index.html.
  std::string_view name = rest.empty() ? std::string_view("index.html") : rest;
  const WebUiFile* file = Find(files, name);
  if (file == nullptr) {
    const std::string_view last = name.substr(name.rfind('/') == std::string_view::npos ? 0 : name.rfind('/') + 1);
    if (name.starts_with("assets/") || last.find('.') != std::string_view::npos) return NotFound();
    name = "index.html";
    file = Find(files, name);
    if (file == nullptr) return NotFound();
  }

  warpsql::PlainResponse response;
  response.content_type = std::string(ContentType(name));
  response.headers.emplace_back("Cache-Control",
                                name.starts_with("assets/") ? "public, max-age=31536000, immutable" : "no-cache");
  response.body.assign(reinterpret_cast<const char*>(file->data), file->size);
  return WithHeaders(std::move(response));
}

}  // namespace hardcharger
