#include "http_client.hpp"

#include <openssl/ssl.h>

#include <boost/asio/co_spawn.hpp>
#include <boost/asio/connect.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/url/parse.hpp>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <stdexcept>

#include "platform.hpp"

namespace hardcharger {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
namespace ssl = asio::ssl;
using tcp = asio::ip::tcp;

namespace {

constexpr int kMaxRedirects = 5;

bool Readable(const char* path) { return std::ifstream(path).good(); }

struct Target {
  bool tls;
  std::string host;
  std::string port;
  std::string path;  // with its query
};

Target Parse(const std::string& url) {
  auto parsed = boost::urls::parse_uri(url);
  if (!parsed) throw std::runtime_error("not a URL: " + url);
  const auto& u = *parsed;
  const std::string scheme(u.scheme());
  if (scheme != "http" && scheme != "https") throw std::runtime_error("not an http(s) URL: " + url);
  Target target{scheme == "https", std::string(u.encoded_host_address()), std::string(u.port()),
                std::string(u.encoded_resource())};
  if (target.host.size() > 1 && target.host.front() == '[') target.host = target.host.substr(1, target.host.size() - 2);
  if (target.port.empty()) target.port = target.tls ? "443" : "80";
  if (target.path.empty()) target.path = "/";
  return target;
}

// Resolves `location` (absolute, or a path) against the URL it came from.
std::string Resolve(const std::string& from, const std::string& location) {
  if (location.find("://") != std::string::npos) return location;
  const Target base = Parse(from);
  const std::string origin = (base.tls ? "https://" : "http://") + base.host + ":" + base.port;
  return origin + (location.starts_with("/") ? location : "/" + location);
}

template <class Stream>
asio::awaitable<HttpResponse> Exchange(Stream& stream, http::request<http::string_body>& request) {
  co_await http::async_write(stream, request, asio::use_awaitable);
  beast::flat_buffer buffer;
  http::response<http::string_body> response;
  co_await http::async_read(stream, buffer, response, asio::use_awaitable);
  HttpResponse out{static_cast<int>(response.result_int()), std::move(response.body()), {}};
  if (out.status >= 300 && out.status < 400) {
    if (auto location = response.find(http::field::location); location != response.end()) {
      out.location = std::string(location->value());
    }
  }
  co_return out;
}

asio::awaitable<HttpResponse> Call(Target target, http::request<http::string_body> request, ssl::context* tls) {
  auto executor = co_await asio::this_coro::executor;
  tcp::resolver resolver(executor);
  const auto endpoints = co_await resolver.async_resolve(target.host, target.port, asio::use_awaitable);
  if (tls == nullptr) {
    beast::tcp_stream stream(executor);
    co_await stream.async_connect(endpoints, asio::use_awaitable);
    auto response = co_await Exchange(stream, request);
    beast::error_code ignored;
    stream.socket().shutdown(tcp::socket::shutdown_both, ignored);
    co_return response;
  }
  beast::ssl_stream<beast::tcp_stream> stream(executor, *tls);
  // SNI, and the certificate must name the host.
  if (!SSL_set_tlsext_host_name(stream.native_handle(), target.host.c_str())) {
    throw std::runtime_error("cannot set the TLS server name for " + target.host);
  }
  stream.set_verify_callback(ssl::host_name_verification(target.host));
  co_await beast::get_lowest_layer(stream).async_connect(endpoints, asio::use_awaitable);
  co_await stream.async_handshake(ssl::stream_base::client, asio::use_awaitable);
  auto response = co_await Exchange(stream, request);
  co_return response;  // no TLS close_notify: some servers never answer it
}

// One request, with `timeout` covering all of it (name lookup included).
HttpResponse Once(const std::string& url, http::verb method, const std::string& body, std::chrono::seconds timeout) {
  const Target target = Parse(url);
  http::request<http::string_body> request(method, target.path, 11);
  request.set(http::field::host, target.host);
  request.set(http::field::user_agent, "Hardcharger");
  request.set(http::field::accept, "application/json");
  if (method == http::verb::post) {
    request.set(http::field::content_type, "application/x-www-form-urlencoded");
    request.body() = body;
    request.prepare_payload();
  }
  std::optional<ssl::context> tls;
  if (target.tls) {
    tls.emplace(ssl::context::tls_client);
    if (auto bundle = FindSystemCaCertFile()) {
      tls->load_verify_file(*bundle);
    } else {
      AddSystemTrustStore(tls->native_handle());  // OpenSSL's default paths; Windows' store
    }
    tls->set_verify_mode(ssl::verify_peer);
  }
  asio::io_context io;
  std::optional<HttpResponse> result;
  std::exception_ptr error;
  asio::co_spawn(io, Call(target, std::move(request), tls ? &*tls : nullptr),
                 [&](std::exception_ptr e, HttpResponse response) {
                   error = e;
                   if (!e) result = std::move(response);
                 });
  io.run_for(timeout);
  if (error) std::rethrow_exception(error);
  if (!result) throw std::runtime_error("no answer within " + std::to_string(timeout.count()) + "s");
  return *result;
}

HttpResponse Fetch(std::string url, http::verb method, const std::string& body, std::chrono::seconds timeout) {
  for (int hop = 0;; hop++) {
    HttpResponse response;
    try {
      response = Once(url, method, body, timeout);
    } catch (const boost::system::system_error& e) {
      throw std::runtime_error("HTTP request to " + url + " failed: " + e.code().message());
    } catch (const std::runtime_error& e) {
      throw std::runtime_error("HTTP request to " + url + " failed: " + e.what());
    }
    const bool redirect = response.status == 301 || response.status == 302 || response.status == 303 ||
                          response.status == 307 || response.status == 308;
    if (!redirect || hop == kMaxRedirects) return response;
    url = Resolve(url, response.location);
    // 303 (and, as browsers do, 301/302 after a POST) continue as a GET.
    if (response.status != 307 && response.status != 308) method = http::verb::get;
  }
}

}  // namespace

std::optional<std::string> FindSystemCaCertFile() {
  if (const char* env = std::getenv("SSL_CERT_FILE"); env != nullptr && *env != '\0' && Readable(env)) {
    return std::string(env);
  }
#ifndef _WIN32  // Windows has no bundle file: its certificate store is read instead
  static constexpr const char* kBundles[] = {
      "/etc/ssl/certs/ca-certificates.crt",    // Debian, Ubuntu, Alpine
      "/etc/pki/tls/certs/ca-bundle.crt",      // RHEL, CentOS, Fedora, Amazon Linux
      "/etc/ssl/ca-bundle.pem",                // OpenSUSE
      "/etc/pki/tls/cacert.pem",               // OpenELEC
      "/etc/ssl/cert.pem",                     // macOS, Alpine, BSD
      "/usr/local/etc/openssl@3/cert.pem",     // Homebrew (Intel)
      "/opt/homebrew/etc/openssl@3/cert.pem",  // Homebrew (Apple Silicon)
  };
  for (const char* path : kBundles) {
    if (Readable(path)) return std::string(path);
  }
#endif
  return std::nullopt;
}

HttpResponse HttpGet(const std::string& url, std::chrono::seconds timeout) {
  return Fetch(url, http::verb::get, {}, timeout);
}

HttpResponse HttpPostForm(const std::string& url, const std::vector<std::pair<std::string, std::string>>& form,
                          std::chrono::seconds timeout) {
  std::string body;
  for (const auto& [key, value] : form) {
    if (!body.empty()) body += '&';
    body += UrlEncode(key) + "=" + UrlEncode(value);
  }
  return Fetch(url, http::verb::post, body, timeout);
}

std::string UrlEncode(std::string_view text) {
  static constexpr char kHex[] = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : text) {
    if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out += static_cast<char>(c);
    } else {
      out += '%';
      out += kHex[c >> 4];
      out += kHex[c & 15];
    }
  }
  return out;
}

}  // namespace hardcharger
