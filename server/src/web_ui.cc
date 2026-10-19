#include "web_ui.hpp"

#include <zlib.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <utility>

namespace hardcharger {

// Everything the UI loads is a file of its own under /ui/. The exceptions, each needed
// (the UI's e2e tests run under this policy and fail on any violation): 'wasm-unsafe-eval'
// for the Parquet export's WebAssembly (not JavaScript eval), inline styles for the editor,
// and data: images in the editor's stylesheet.
const std::string_view kWebUiContentSecurityPolicy =
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

namespace {

std::string_view Trim(std::string_view s) {
  while (!s.empty() && (s.front() == ' ' || s.front() == '\t')) s.remove_prefix(1);
  while (!s.empty() && (s.back() == ' ' || s.back() == '\t')) s.remove_suffix(1);
  return s;
}

bool IEquals(std::string_view a, std::string_view b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](char x, char y) {
           return std::tolower(static_cast<unsigned char>(x)) == std::tolower(static_cast<unsigned char>(y));
         });
}

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

// The file's own bytes from its embedded gzip.
std::string Inflate(const WebUiFile& file) {
  std::string out(file.raw_size, '\0');
  z_stream stream{};
  if (inflateInit2(&stream, 16 + MAX_WBITS) != Z_OK) throw std::runtime_error("web UI: inflateInit2 failed");
  stream.next_in = const_cast<Bytef*>(file.data);
  stream.avail_in = static_cast<uInt>(file.size);
  stream.next_out = reinterpret_cast<Bytef*>(out.data());
  stream.avail_out = static_cast<uInt>(out.size());
  const int status = inflate(&stream, Z_FINISH);
  const auto produced = stream.total_out;
  inflateEnd(&stream);
  if (status != Z_STREAM_END || produced != file.raw_size) {
    throw std::runtime_error("web UI: an embedded file does not inflate to its size: " + std::string(file.path));
  }
  return out;
}

// Whether If-None-Match names `etag` (weak comparison, as RFC 9110 has it for this header).
bool NoneMatch(std::string_view header, std::string_view etag) {
  if (header.empty()) return false;
  while (!header.empty()) {
    const auto comma = header.find(',');
    std::string_view tag = Trim(header.substr(0, comma));
    header = comma == std::string_view::npos ? std::string_view{} : header.substr(comma + 1);
    if (tag == "*") return true;
    if (tag.starts_with("W/")) tag.remove_prefix(2);
    if (tag == etag) return true;
  }
  return false;
}

}  // namespace

std::optional<std::string> EmbeddedWebUiFileContent(std::span<const WebUiFile> files, std::string_view path) {
  const WebUiFile* file = Find(files, path);
  if (file == nullptr) return std::nullopt;
  if (file->gzipped) return Inflate(*file);
  return std::string(reinterpret_cast<const char*>(file->data), file->size);
}

bool AcceptsGzip(std::string_view accept_encoding) {
  std::optional<double> gzip, any;
  while (!accept_encoding.empty()) {
    const auto comma = accept_encoding.find(',');
    std::string_view item = Trim(accept_encoding.substr(0, comma));
    accept_encoding = comma == std::string_view::npos ? std::string_view{} : accept_encoding.substr(comma + 1);
    double q = 1;
    const auto semicolon = item.find(';');
    const std::string_view coding = Trim(item.substr(0, semicolon));
    if (semicolon != std::string_view::npos) {
      std::string_view params = item.substr(semicolon + 1);
      while (!params.empty()) {
        const auto next = params.find(';');
        const std::string_view param = Trim(params.substr(0, next));
        params = next == std::string_view::npos ? std::string_view{} : params.substr(next + 1);
        if (param.size() > 2 && (param[0] == 'q' || param[0] == 'Q') && param[1] == '=') {
          q = std::strtod(std::string(param.substr(2)).c_str(), nullptr);
        }
      }
    }
    if (IEquals(coding, "gzip") || IEquals(coding, "x-gzip")) {
      gzip = q;
    } else if (coding == "*") {
      any = q;
    }
  }
  if (gzip) return *gzip > 0;
  return any && *any > 0;
}

std::optional<warpsql::PlainResponse> ServeWebUi(std::span<const WebUiFile> files,
                                                 const warpsql::PlainRequest& request) {
  const std::string_view path = request.path;
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

  // Two representations of a gzipped file, each with its own strong ETag.
  const bool gzip = file->gzipped && AcceptsGzip(request.Header("Accept-Encoding"));
  const std::string etag =
      "\"" + std::string(file->sha256.substr(0, 32)) + (gzip ? "-gzip" : "") + "\"";
  warpsql::PlainResponse response;
  response.headers.emplace_back("Cache-Control",
                                name.starts_with("assets/") ? "public, max-age=31536000, immutable" : "no-cache");
  response.headers.emplace_back("ETag", etag);
  if (file->gzipped) response.headers.emplace_back("Vary", "Accept-Encoding");
  if (NoneMatch(request.Header("If-None-Match"), etag)) {
    response.status = 304;
    return WithHeaders(std::move(response));
  }
  response.content_type = std::string(ContentType(name));
  if (gzip) {
    response.headers.emplace_back("Content-Encoding", "gzip");
    response.body.assign(reinterpret_cast<const char*>(file->data), file->size);
  } else if (file->gzipped) {
    response.body = Inflate(*file);
  } else {
    response.body.assign(reinterpret_cast<const char*>(file->data), file->size);
  }
  return WithHeaders(std::move(response));
}

}  // namespace hardcharger
