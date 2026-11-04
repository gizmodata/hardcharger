#include "web_relay.hpp"

#include <openssl/evp.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

#include <algorithm>
#include <boost/asio.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/json.hpp>
#include <boost/url/parse.hpp>
#include <cctype>
#include <cstring>
#include <fstream>
#include <map>
#include <mutex>
#include <optional>
#include <random>
#include <stdexcept>

#include "platform.hpp"

namespace hardcharger::web {

namespace net = boost::asio;
namespace beast = boost::beast;
namespace http = boost::beast::http;
namespace ssl = boost::asio::ssl;
namespace json = boost::json;
using tcp = net::ip::tcp;
using Clock = std::chrono::steady_clock;
template <typename T>
using Task = net::awaitable<T>;

namespace {

constexpr std::string_view kPrefix = "/ui/v1/";
constexpr std::string_view kRemotesPath = "/ui/v1/remotes";
constexpr std::string_view kSessionHeader = "Chargewire-Relay-Session";
constexpr std::string_view kAuthorizationHeader = "Chargewire-Relay-Authorization";
constexpr std::size_t kMaxJsonBody = 64 * 1024;
constexpr std::size_t kReadBuffer = 256 * 1024;

enum class TlsMode { kVerified, kFingerprint, kPlaintext };

std::string_view TlsModeName(TlsMode mode) {
  switch (mode) {
    case TlsMode::kVerified:
      return "verified";
    case TlsMode::kFingerprint:
      return "fingerprint";
    case TlsMode::kPlaintext:
      return "plaintext";
  }
  return "";
}

std::optional<TlsMode> ParseTlsMode(std::string_view text) {
  if (text == "verified") return TlsMode::kVerified;
  if (text == "fingerprint") return TlsMode::kFingerprint;
  if (text == "plaintext") return TlsMode::kPlaintext;
  return std::nullopt;
}

// A problem the relay itself answers (RFC 9457, as the protocol's own).
struct Problem : std::runtime_error {
  Problem(int status, std::string code, std::string detail)
      : std::runtime_error(std::move(detail)), status(status), code(std::move(code)) {}
  int status;
  std::string code;
};

std::string Lower(std::string s) {
  for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

bool IEquals(std::string_view a, std::string_view b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](char x, char y) {
           return std::tolower(static_cast<unsigned char>(x)) == std::tolower(static_cast<unsigned char>(y));
         });
}

std::string RandomHex(std::size_t bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::random_device device;
  std::string out;
  for (std::size_t i = 0; i < bytes; i += 4) {
    const unsigned int word = device();
    for (int shift = 0; shift < 32 && out.size() < bytes * 2; shift += 8) {
      const unsigned char byte = static_cast<unsigned char>(word >> shift);
      out += kHex[byte >> 4];
      out += kHex[byte & 15];
    }
  }
  return out;
}

std::string Sha256Hex(std::string_view text) {
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int size = 0;
  EVP_Digest(text.data(), text.size(), digest, &size, EVP_sha256(), nullptr);
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  for (unsigned int i = 0; i < size; i++) {
    out += kHex[digest[i] >> 4];
    out += kHex[digest[i] & 15];
  }
  return out;
}

// A request body read whole, for the relay's own JSON endpoints.
Task<std::string> ReadWhole(chargewire::AppRequest& request, std::size_t limit) {
  std::string body;
  if (!request.body) co_return body;
  while (auto piece = co_await request.body->Next()) {
    body += *piece;
    if (body.size() > limit) throw Problem(413, "payload_too_large", "the request body is too large");
  }
  co_return body;
}

json::object ParseObject(const std::string& text) {
  boost::system::error_code ec;
  json::value value = json::parse(text, ec);
  if (ec || !value.is_object()) throw Problem(400, "bad_request", "the request body must be a JSON object");
  return std::move(value.as_object());
}

std::string StringField(const json::object& object, std::string_view name, bool required) {
  const json::value* value = object.if_contains(name);
  if (value == nullptr || value->is_null()) {
    if (required) throw Problem(400, "bad_request", std::string("'") + std::string(name) + "' is required");
    return {};
  }
  if (!value->is_string()) throw Problem(400, "bad_request", std::string("'") + std::string(name) + "' must be a string");
  return std::string(value->as_string());
}

chargewire::AppResponse JsonAnswer(int status, const json::value& body) {
  chargewire::AppResponse response;
  response.status = status;
  response.content_type = "application/json";
  response.headers.emplace_back("Cache-Control", "no-store");
  response.headers.emplace_back("X-Content-Type-Options", "nosniff");
  response.body = json::serialize(body);
  return response;
}

chargewire::AppResponse ProblemAnswer(const Problem& problem) {
  chargewire::AppResponse response;
  response.status = problem.status;
  response.content_type = "application/problem+json";
  response.headers.emplace_back("Cache-Control", "no-store");
  response.headers.emplace_back("X-Content-Type-Options", "nosniff");
  if (problem.status == 401) response.headers.emplace_back("WWW-Authenticate", "Bearer");
  response.body = json::serialize(json::object{{"type", "urn:hardcharger:error:" + problem.code},
                                               {"title", problem.code},
                                               {"status", problem.status},
                                               {"code", problem.code},
                                               {"detail", problem.what()}});
  return response;
}

// Where a remote is: its scheme, host, port and the base path the protocol is mounted at
// (spec §2.1: the URL's path, else /chargewire).
struct Target {
  bool https = false;
  std::string host;
  std::string port;
  std::string base_path;  // "/chargewire" by default; never ends with '/'
  std::string origin;     // scheme://host:port
};

Target ParseRemoteUrl(const std::string& url) {
  auto parsed = boost::urls::parse_uri(url);
  if (!parsed) throw Problem(400, "bad_request", "the server URL is not a URL: " + url);
  const auto& u = *parsed;
  const std::string scheme = Lower(std::string(u.scheme()));
  Target target;
  if (scheme == "https" || scheme == "chargewire") {
    target.https = true;
  } else if (scheme == "http") {
    target.https = false;
  } else {
    throw Problem(400, "bad_request", "the server URL must start with https:// or http://");
  }
  if (u.has_userinfo()) throw Problem(400, "bad_request", "the server URL must not carry credentials");
  if (u.has_query() || u.has_fragment()) {
    throw Problem(400, "bad_request", "the server URL must not carry a query or fragment");
  }
  target.host = std::string(u.encoded_host_address());
  if (target.host.empty()) throw Problem(400, "bad_request", "the server URL names no host");
  target.port = std::string(u.port());
  if (target.port.empty()) target.port = target.https ? "443" : "80";
  if (scheme == "chargewire" && !u.has_port()) target.port = "31337";
  std::string path = std::string(u.encoded_path());
  while (!path.empty() && path.back() == '/') path.pop_back();
  target.base_path = path.empty() ? "/chargewire" : path;
  const bool v6 = target.host.find(':') != std::string::npos && !target.host.starts_with('[');
  target.origin = (target.https ? "https://" : "http://") + (v6 ? "[" + target.host + "]" : target.host) + ":" +
                  target.port;
  return target;
}

struct CertificateInfo {
  std::string fingerprint;  // normalized
  std::string subject;
  std::string issuer;
  std::string not_after;
  std::vector<std::string> names;  // DNS and IP subject alternative names
  bool verified = false;           // chained to a trusted root and named the host
};

std::string OneLine(X509_NAME* name) {
  if (name == nullptr) return {};
  BIO* bio = BIO_new(BIO_s_mem());
  X509_NAME_print_ex(bio, name, 0, XN_FLAG_RFC2253);
  char* data = nullptr;
  const long size = BIO_get_mem_data(bio, &data);
  std::string out(data != nullptr && size > 0 ? std::string(data, static_cast<std::size_t>(size)) : std::string());
  BIO_free(bio);
  return out;
}

std::string Rfc3339(const ASN1_TIME* when) {
  if (when == nullptr) return {};
  struct tm parts{};
  if (ASN1_TIME_to_tm(when, &parts) != 1) return {};
  char out[32];
  const std::size_t n = std::strftime(out, sizeof(out), "%Y-%m-%dT%H:%M:%SZ", &parts);
  return std::string(out, n);
}

CertificateInfo DescribeCertificate(X509* cert) {
  CertificateInfo info;
  if (cert == nullptr) return info;
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int size = 0;
  if (X509_digest(cert, EVP_sha256(), digest, &size) == 1) {
    static constexpr char kHex[] = "0123456789abcdef";
    for (unsigned int i = 0; i < size; i++) {
      if (i > 0) info.fingerprint += ':';
      info.fingerprint += kHex[digest[i] >> 4];
      info.fingerprint += kHex[digest[i] & 15];
    }
  }
  info.subject = OneLine(X509_get_subject_name(cert));
  info.issuer = OneLine(X509_get_issuer_name(cert));
  info.not_after = Rfc3339(X509_get0_notAfter(cert));
  auto* names = static_cast<GENERAL_NAMES*>(X509_get_ext_d2i(cert, NID_subject_alt_name, nullptr, nullptr));
  if (names != nullptr) {
    for (int i = 0; i < sk_GENERAL_NAME_num(names); i++) {
      const GENERAL_NAME* name = sk_GENERAL_NAME_value(names, i);
      if (name->type == GEN_DNS) {
        const unsigned char* data = ASN1_STRING_get0_data(name->d.dNSName);
        info.names.emplace_back(reinterpret_cast<const char*>(data),
                                static_cast<std::size_t>(ASN1_STRING_length(name->d.dNSName)));
      } else if (name->type == GEN_IPADD) {
        const unsigned char* data = ASN1_STRING_get0_data(name->d.iPAddress);
        const int length = ASN1_STRING_length(name->d.iPAddress);
        if (length == 4) {
          info.names.push_back(std::to_string(data[0]) + "." + std::to_string(data[1]) + "." +
                               std::to_string(data[2]) + "." + std::to_string(data[3]));
        } else if (length == 16) {
          std::array<unsigned char, 16> bytes{};
          std::memcpy(bytes.data(), data, 16);
          info.names.push_back(net::ip::make_address_v6(bytes).to_string());
        }
      }
    }
    GENERAL_NAMES_free(names);
  }
  return info;
}

json::object CertificateJson(const CertificateInfo& info, TlsMode mode) {
  json::array names;
  for (const auto& name : info.names) names.emplace_back(name);
  return json::object{{"mode", TlsModeName(mode)},
                      {"fingerprint", info.fingerprint},
                      {"subject", info.subject},
                      {"issuer", info.issuer},
                      {"not_after", info.not_after},
                      {"names", std::move(names)},
                      {"verified", info.verified}};
}

// What the relay knows about a registered remote.
struct Remote {
  std::string id;
  std::string name;
  std::string url;  // as given
  Target target;
  TlsMode tls = TlsMode::kVerified;
  std::string fingerprint;  // normalized; in fingerprint mode, the one trusted
  json::object server;      // the remote's discovery "server" block
  json::object certificate;  // CertificateJson at registration (plaintext: empty)
};

struct RelaySession {
  std::string key;
  std::string owner;  // who it belongs to (a local principal, or "remote:<url>" once signed in)
  bool signed_in = false;
  std::map<std::string, std::shared_ptr<Remote>> remotes;
  Clock::time_point expires;
};

// The headers that never cross the relay (RFC 9110 §7.6.1 hop-by-hop, the browser's own
// and the relay's).
bool DroppedRequestHeader(std::string_view name) {
  for (const char* dropped : {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                              "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "content-length",
                              "cookie", "authorization", "origin", "referer", "sec-fetch-site", "sec-fetch-mode",
                              "sec-fetch-dest", "sec-fetch-user", "accept-encoding"}) {
    if (IEquals(name, dropped)) return true;
  }
  return name.size() > 16 && IEquals(name.substr(0, 17), "chargewire-relay-");
}

bool DroppedResponseHeader(std::string_view name) {
  for (const char* dropped : {"connection", "keep-alive", "proxy-authenticate", "proxy-connection", "te", "trailer",
                              "transfer-encoding", "upgrade", "content-length", "set-cookie", "set-cookie2",
                              "access-control-allow-origin", "access-control-allow-credentials",
                              "access-control-expose-headers", "content-encoding"}) {
    if (IEquals(name, dropped)) return true;
  }
  return false;
}

// Only what the UI may do through a relay session that nobody has signed in through yet:
// find out what the remote is, and sign in to it.
bool AllowedWhilePending(std::string_view method, std::string_view path) {
  if (path.empty() || path == "/") return method == "GET";
  if (path == "/v1/auth/token") return method == "POST";
  if (path.starts_with("/v1/auth/oauth/")) return true;
  if (path == "/v1/openapi.json" || path == "/v1/health") return method == "GET";
  return false;
}

std::string SslErrorText(const boost::system::error_code& ec) {
  std::string text = ec.message();
  // OpenSSL's "certificate verify failed" says nothing about why: add the verify result.
  return text;
}

}  // namespace

std::string NormalizeFingerprint(std::string_view text) {
  std::string hex;
  for (char c : text) {
    if (c == ':' || c == ' ' || c == '-') continue;
    if (!std::isxdigit(static_cast<unsigned char>(c))) return {};
    hex += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  if (hex.size() != 64) return {};
  std::string out;
  for (std::size_t i = 0; i < hex.size(); i += 2) {
    if (i > 0) out += ':';
    out += hex.substr(i, 2);
  }
  return out;
}

class Relay::Impl {
 public:
  explicit Impl(RelayOptions options) : options_(std::move(options)) {}

  std::vector<chargewire::AppEndpoint> Endpoints() {
    chargewire::AppEndpoint endpoint;
    endpoint.prefix = std::string(kPrefix);
    endpoint.requires_token = false;  // the relay session decides (Authorize)
    endpoint.handler = [this](chargewire::AppRequest& request) -> Task<chargewire::AppResponse> {
      try {
        co_return co_await Route(request);
      } catch (const Problem& problem) {
        co_return ProblemAnswer(problem);
      } catch (const boost::system::system_error& error) {
        co_return ProblemAnswer(Problem(502, "remote_unreachable", error.code().message()));
      } catch (const std::exception& error) {
        co_return ProblemAnswer(Problem(500, "internal_error", error.what()));
      }
    };
    return {std::move(endpoint)};
  }

  std::size_t SessionCount() const {
    std::lock_guard lock(mutex_);
    return sessions_.size();
  }

 private:
  // ---- Routing ----

  Task<chargewire::AppResponse> Route(chargewire::AppRequest& request) {
    const std::string& path = request.path;
    if (path == "/ui/v1/app") {
      if (request.method != "GET") throw Problem(405, "method_not_allowed", "this endpoint takes GET");
      co_return Contract();
    }
    if (!path.starts_with(kRemotesPath)) throw Problem(404, "not_found", "no such endpoint");
    if (!options_.remote_servers) {
      throw Problem(404, "not_found",
                    "remote servers are off on this server (web_ui_remote_servers is false)");
    }
    std::string rest = path.substr(kRemotesPath.size());  // "", "/probe", "/<id>", "/<id>/chargewire/..."
    if (rest.empty() || rest == "/") {
      if (request.method == "GET") co_return ListRemotes(request);
      if (request.method == "POST") co_return co_await AddRemote(request);
      throw Problem(405, "method_not_allowed", "this endpoint takes GET or POST");
    }
    if (rest == "/probe") {
      if (request.method != "POST") throw Problem(405, "method_not_allowed", "this endpoint takes POST");
      co_return co_await Probe(request);
    }
    rest.erase(0, 1);
    const auto slash = rest.find('/');
    const std::string id = rest.substr(0, slash);
    const std::string below = slash == std::string::npos ? std::string() : rest.substr(slash);
    if (below.empty()) {
      if (request.method != "DELETE") throw Problem(405, "method_not_allowed", "this endpoint takes DELETE");
      co_return RemoveRemote(request, id);
    }
    if (below != "/chargewire" && !below.starts_with("/chargewire/")) {
      throw Problem(404, "not_found", "only the remote's /chargewire paths are relayed");
    }
    co_return co_await Forward(request, id, below.substr(std::string_view("/chargewire").size()));
  }

  chargewire::AppResponse Contract() const {
    return JsonAnswer(200, json::object{{"version", options_.version},
                                        {"local_server", options_.local_server},
                                        {"remote_servers", options_.remote_servers}});
  }

  // ---- Relay sessions ----

  // The relay session this request may use: the one its Chargewire-Relay-Session names, else
  // the one its (validated) bearer token stands for, made on first use. `make_pending`: a
  // request with neither may make a new, pending session (registering a remote). The
  // session is returned under the lock released: a copy of what the caller needs.
  struct Authorized {
    std::string key;
    bool signed_in = false;
    bool created = false;
  };

  Authorized Authorize(const chargewire::AppRequest& request, bool make_pending) {
    const auto now = Clock::now();
    std::lock_guard lock(mutex_);
    Sweep(now);
    const std::string_view named = request.Header(kSessionHeader);
    if (!named.empty()) {
      auto it = sessions_.find(std::string(named));
      if (it != sessions_.end()) {
        Touch(*it->second, now);
        return {it->first, it->second->signed_in, false};
      }
      if (!request.principal && !make_pending) {
        throw Problem(401, "relay_session_required", "the relay session has ended: add the server again");
      }
    }
    if (request.principal) {
      // The UI's own sign-in: one session per access token, as long as the token.
      const std::string key = "t-" + Sha256Hex(BearerOf(request));
      auto it = sessions_.find(key);
      if (it == sessions_.end()) {
        it = sessions_.emplace(key, Make(key, request.principal->name, true, now)).first;
      }
      Touch(*it->second, now);
      return {key, true, false};
    }
    if (!make_pending) {
      throw Problem(401, "relay_session_required",
                    "sign in first, or register the server to start a relay session");
    }
    const std::string key = "p-" + RandomHex(24);
    auto session = Make(key, "", false, now);
    sessions_.emplace(key, std::move(session));
    return {key, false, true};
  }

  static std::string BearerOf(const chargewire::AppRequest& request) {
    std::string_view header = request.Header("Authorization");
    if (header.size() > 7 && IEquals(header.substr(0, 7), "Bearer ")) header.remove_prefix(7);
    return std::string(header);
  }

  std::shared_ptr<RelaySession> Make(const std::string& key, std::string owner, bool signed_in,
                                     Clock::time_point now) {
    if (sessions_.size() >= options_.max_sessions) {
      throw Problem(429, "rate_limited", "too many relay sessions on this server; try again later");
    }
    auto session = std::make_shared<RelaySession>();
    session->key = key;
    session->owner = std::move(owner);
    session->signed_in = signed_in;
    session->expires = now + (signed_in ? options_.session_lifetime : options_.pending_lifetime);
    return session;
  }

  void Touch(RelaySession& session, Clock::time_point now) {
    session.expires = now + (session.signed_in ? options_.session_lifetime : options_.pending_lifetime);
  }

  void Sweep(Clock::time_point now) {
    if (now < next_sweep_) return;
    next_sweep_ = now + std::chrono::seconds(30);
    for (auto it = sessions_.begin(); it != sessions_.end();) {
      if (it->second->expires <= now) {
        it = sessions_.erase(it);
      } else {
        ++it;
      }
    }
  }

  std::shared_ptr<RelaySession> Session(const std::string& key) {
    std::lock_guard lock(mutex_);
    auto it = sessions_.find(key);
    if (it == sessions_.end()) throw Problem(401, "relay_session_required", "the relay session has ended");
    return it->second;
  }

  std::shared_ptr<Remote> RemoteOf(const std::string& key, const std::string& id) {
    std::lock_guard lock(mutex_);
    auto it = sessions_.find(key);
    if (it == sessions_.end()) throw Problem(401, "relay_session_required", "the relay session has ended");
    auto remote = it->second->remotes.find(id);
    if (remote == it->second->remotes.end()) throw Problem(404, "remote_not_found", "no such remote server: " + id);
    return remote->second;
  }

  // ---- The remotes API ----

  json::object RemoteJson(const Remote& remote, const std::string& session_key) const {
    return json::object{{"id", remote.id},
                        {"name", remote.name},
                        {"url", remote.url},
                        {"origin", remote.target.origin},
                        {"base_path", remote.target.base_path},
                        {"tls", remote.tls == TlsMode::kPlaintext ? json::value(nullptr) : json::value(remote.certificate)},
                        {"tls_mode", TlsModeName(remote.tls)},
                        {"server", remote.server},
                        {"relay_session", session_key},
                        {"relay_path", std::string(kRemotesPath) + "/" + remote.id + "/chargewire"}};
  }

  chargewire::AppResponse ListRemotes(chargewire::AppRequest& request) {
    const Authorized authorized = Authorize(request, false);
    auto session = Session(authorized.key);
    json::array remotes;
    {
      std::lock_guard lock(mutex_);
      for (const auto& [id, remote] : session->remotes) remotes.push_back(RemoteJson(*remote, session->key));
    }
    return JsonAnswer(200, json::object{{"remotes", std::move(remotes)},
                                        {"relay_session", session->key},
                                        {"signed_in", session->signed_in}});
  }

  struct RemoteRequest {
    std::string name;
    std::string url;
    TlsMode tls = TlsMode::kVerified;
    std::string fingerprint;
    Target target;
  };

  Task<RemoteRequest> ReadRemoteRequest(chargewire::AppRequest& request) {
    const json::object body = ParseObject(co_await ReadWhole(request, kMaxJsonBody));
    RemoteRequest out;
    out.url = StringField(body, "url", true);
    out.name = StringField(body, "name", false);
    out.target = ParseRemoteUrl(out.url);
    const std::string mode = StringField(body, "tls", false);
    if (mode.empty()) {
      out.tls = out.target.https ? TlsMode::kVerified : TlsMode::kPlaintext;
    } else if (auto parsed = ParseTlsMode(mode)) {
      out.tls = *parsed;
    } else {
      throw Problem(400, "bad_request", "'tls' must be verified, fingerprint or plaintext");
    }
    if (out.tls == TlsMode::kPlaintext && out.target.https) {
      throw Problem(400, "bad_request", "an https:// server can't be reached in plaintext");
    }
    if (out.tls != TlsMode::kPlaintext && !out.target.https) {
      throw Problem(400, "bad_request", "an http:// server has no certificate to verify or trust");
    }
    const std::string given = StringField(body, "fingerprint", false);
    if (!given.empty()) {
      out.fingerprint = NormalizeFingerprint(given);
      if (out.fingerprint.empty()) throw Problem(400, "bad_request", "'fingerprint' is not a SHA-256 fingerprint");
    }
    if (out.name.empty()) out.name = out.target.host;
    co_return out;
  }

  // What a probe or a registration learns about the remote.
  struct Probed {
    json::object discovery;
    CertificateInfo certificate;
  };

  // Connects to the remote, checks (or reads) its certificate, and reads its discovery
  // document (spec §3.1). Throws a Problem naming what went wrong.
  Task<Probed> ProbeRemote(const RemoteRequest& spec) {
    Probed probed;
    auto handle = [&](int status, std::string body, CertificateInfo certificate) {
      probed.certificate = std::move(certificate);
      if (status != 200) {
        throw Problem(502, "remote_not_chargewire",
                      spec.target.origin + spec.target.base_path + " answered " + std::to_string(status) +
                          ", not a Chargewire discovery document");
      }
      boost::system::error_code ec;
      json::value value = json::parse(body, ec);
      if (ec || !value.is_object() || !value.as_object().if_contains("protocol") ||
          !value.as_object().at("protocol").is_string() || value.as_object().at("protocol").as_string() != "chargewire") {
        throw Problem(502, "remote_not_chargewire", spec.target.origin + " is not a Chargewire server");
      }
      probed.discovery = std::move(value.as_object());
    };
    if (spec.tls == TlsMode::kPlaintext) {
      beast::tcp_stream stream(co_await net::this_coro::executor);
      co_await Connect(stream, spec.target);
      auto [status, body] = co_await Get(stream, spec.target, spec.target.base_path);
      handle(status, std::move(body), {});
      co_return probed;
    }
    beast::ssl_stream<beast::tcp_stream> stream(co_await net::this_coro::executor, *Context(spec.tls));
    CertificateInfo certificate = co_await Handshake(stream, spec.target, spec.tls, spec.fingerprint, true);
    auto [status, body] = co_await Get(stream, spec.target, spec.target.base_path);
    handle(status, std::move(body), std::move(certificate));
    co_return probed;
  }

  Task<chargewire::AppResponse> Probe(chargewire::AppRequest& request) {
    // Anyone the listener admits may ask what a server is: the answer is the remote's own
    // public discovery document and certificate, nothing of this server's.
    const RemoteRequest spec = co_await ReadRemoteRequest(request);
    Probed probed = co_await ProbeRemote(spec);
    const json::value* server = probed.discovery.if_contains("server");
    co_return JsonAnswer(200, json::object{
                                  {"url", spec.url},
                                  {"origin", spec.target.origin},
                                  {"base_path", spec.target.base_path},
                                  {"tls_mode", TlsModeName(spec.tls)},
                                  {"tls", spec.tls == TlsMode::kPlaintext ? json::value(nullptr)
                                                                            : json::value(CertificateJson(probed.certificate, spec.tls))},
                                  {"server", server != nullptr && server->is_object() ? server->as_object() : json::object{}},
                                  {"auth", probed.discovery.if_contains("auth") != nullptr ? probed.discovery.at("auth") : json::value(nullptr)},
                                  {"capabilities", probed.discovery.if_contains("capabilities") != nullptr
                                                       ? probed.discovery.at("capabilities")
                                                       : json::value(json::array{})}});
  }

  Task<chargewire::AppResponse> AddRemote(chargewire::AppRequest& request) {
    const RemoteRequest spec = co_await ReadRemoteRequest(request);
    if (spec.tls == TlsMode::kFingerprint && spec.fingerprint.empty()) {
      throw Problem(400, "bad_request", "trusting a certificate by fingerprint needs its 'fingerprint' (probe the server first)");
    }
    // Authorize before connecting anywhere: a request that may not use the relay costs no
    // connection. Where there is nothing local to sign in to, a request without a session
    // starts a pending one, made only once the remote has answered; on a server people
    // sign in to, the relay is theirs alone.
    const bool pending_allowed = !options_.local_server;
    const bool has_session = !request.Header(kSessionHeader).empty() || request.principal;
    Authorized authorized;
    if (has_session || !pending_allowed) authorized = Authorize(request, pending_allowed);
    Probed probed = co_await ProbeRemote(spec);
    if (!has_session && pending_allowed) authorized = Authorize(request, true);
    auto remote = std::make_shared<Remote>();
    remote->id = "r-" + RandomHex(8);
    remote->name = spec.name;
    remote->url = spec.url;
    remote->target = spec.target;
    remote->tls = spec.tls;
    remote->fingerprint = spec.tls == TlsMode::kPlaintext ? std::string() : probed.certificate.fingerprint;
    if (const json::value* server = probed.discovery.if_contains("server"); server != nullptr && server->is_object()) {
      remote->server = server->as_object();
    }
    if (spec.tls != TlsMode::kPlaintext) remote->certificate = CertificateJson(probed.certificate, spec.tls);
    json::object answer;
    {
      std::lock_guard lock(mutex_);
      auto it = sessions_.find(authorized.key);
      if (it == sessions_.end()) throw Problem(401, "relay_session_required", "the relay session has ended");
      auto& session = *it->second;
      // The same server registered again (a reload) replaces the earlier entry.
      for (auto r = session.remotes.begin(); r != session.remotes.end();) {
        if (r->second->target.origin == remote->target.origin && r->second->target.base_path == remote->target.base_path) {
          r = session.remotes.erase(r);
        } else {
          ++r;
        }
      }
      if (session.remotes.size() >= options_.max_remotes_per_session) {
        throw Problem(429, "rate_limited",
                      "at most " + std::to_string(options_.max_remotes_per_session) + " remote servers at once");
      }
      session.remotes[remote->id] = remote;
      answer = RemoteJson(*remote, session.key);
      answer["signed_in"] = session.signed_in;
    }
    Log(false, "web UI: remote server added",
        {{"remote", remote->target.origin}, {"tls", std::string(TlsModeName(remote->tls))},
         {"user", request.principal ? request.principal->name : std::string("(no local sign-in)")},
         {"peer", request.peer}});
    co_return JsonAnswer(201, answer);
  }

  chargewire::AppResponse RemoveRemote(chargewire::AppRequest& request, const std::string& id) {
    const Authorized authorized = Authorize(request, false);
    {
      std::lock_guard lock(mutex_);
      auto it = sessions_.find(authorized.key);
      if (it == sessions_.end()) throw Problem(401, "relay_session_required", "the relay session has ended");
      if (it->second->remotes.erase(id) == 0) throw Problem(404, "remote_not_found", "no such remote server: " + id);
    }
    chargewire::AppResponse response;
    response.status = 204;
    response.headers.emplace_back("Cache-Control", "no-store");
    return response;
  }

  // ---- Forwarding ----

  Task<chargewire::AppResponse> Forward(chargewire::AppRequest& request, const std::string& id,
                                        const std::string& below) {
    const Authorized authorized = Authorize(request, false);
    auto remote = RemoteOf(authorized.key, id);
    if (!authorized.signed_in && !AllowedWhilePending(request.method, below)) {
      throw Problem(401, "relay_session_required", "sign in to the remote server first");
    }
    // The remote's path: the base path, what follows /chargewire here, and the query as sent.
    std::string target = remote->target.base_path + below;
    if (const auto q = request.target.find('?'); q != std::string::npos) target += request.target.substr(q);

    http::request<http::empty_body> upstream{http::string_to_verb(request.method), target, 11};
    upstream.set(http::field::host, remote->target.host + ":" + remote->target.port);
    upstream.set(http::field::user_agent, "hardcharger-web-relay/" + options_.version);
    for (const auto& [name, value] : request.headers) {
      if (!DroppedRequestHeader(name)) upstream.set(name, value);
    }
    if (const std::string_view token = request.Header(kAuthorizationHeader); !token.empty()) {
      upstream.set(http::field::authorization, std::string(token));
    }
    std::optional<std::uint64_t> length;
    bool chunked = false;
    if (const std::string_view given = request.Header("Content-Length"); !given.empty()) {
      length = std::stoull(std::string(given));
      upstream.content_length(*length);
    } else if (IEquals(request.Header("Transfer-Encoding"), "chunked")) {
      chunked = true;
      upstream.chunked(true);
    }
    const bool has_body = request.body && (chunked || (length && *length > 0));
    const bool sign_in = !authorized.signed_in && below == "/v1/auth/token";

    chargewire::AppResponse response;
    if (remote->tls == TlsMode::kPlaintext) {
      auto stream = std::make_shared<beast::tcp_stream>(co_await net::this_coro::executor);
      co_await Connect(*stream, remote->target);
      response = co_await Exchange(stream, std::move(upstream), has_body ? request.body : nullptr, chunked);
    } else {
      auto stream = std::make_shared<beast::ssl_stream<beast::tcp_stream>>(co_await net::this_coro::executor,
                                                                          *Context(remote->tls));
      co_await Handshake(*stream, remote->target, remote->tls, remote->fingerprint, false);
      response = co_await Exchange(stream, std::move(upstream), has_body ? request.body : nullptr, chunked);
    }
    if (sign_in && response.status == 200) {
      std::lock_guard lock(mutex_);
      auto it = sessions_.find(authorized.key);
      if (it != sessions_.end() && !it->second->signed_in) {
        it->second->signed_in = true;
        it->second->owner = "remote:" + remote->target.origin;
        Touch(*it->second, Clock::now());
      }
      Log(false, "web UI: signed in to a remote server through the relay",
          {{"remote", remote->target.origin}, {"peer", request.peer}});
    }
    co_return response;
  }

  // Sends `upstream` (and `body`, streamed) and reads the answer's headers; the answer's
  // body streams to the browser as it arrives.
  template <class Stream>
  Task<chargewire::AppResponse> Exchange(std::shared_ptr<Stream> stream, http::request<http::empty_body> upstream,
                                         std::shared_ptr<chargewire::AppBody> body, bool chunked) {
    auto& lowest = beast::get_lowest_layer(*stream);
    lowest.expires_after(options_.body_timeout);
    http::request_serializer<http::empty_body> serializer{upstream};
    co_await http::async_write_header(*stream, serializer, net::use_awaitable);
    if (body) {
      while (auto piece = co_await body->Next()) {
        if (piece->empty()) continue;
        lowest.expires_after(options_.body_timeout);
        if (chunked) {
          co_await net::async_write(*stream, http::make_chunk(net::buffer(*piece)), net::use_awaitable);
        } else {
          co_await net::async_write(*stream, net::buffer(*piece), net::use_awaitable);
        }
      }
      if (chunked) co_await net::async_write(*stream, http::make_chunk_last(), net::use_awaitable);
    }
    auto buffer = std::make_shared<beast::flat_buffer>();
    buffer->reserve(kReadBuffer);
    auto parser = std::make_shared<http::response_parser<http::buffer_body>>();
    parser->body_limit(boost::none);
    lowest.expires_after(options_.response_timeout);
    co_await http::async_read_header(*stream, *buffer, *parser, net::use_awaitable);

    chargewire::AppResponse response;
    const auto& message = parser->get();
    response.status = static_cast<int>(message.result_int());
    for (const auto& field : message) {
      const std::string name(field.name_string());
      if (IEquals(name, "content-type")) {
        response.content_type = std::string(field.value());
      } else if (!DroppedResponseHeader(name)) {
        response.headers.emplace_back(name, std::string(field.value()));
      }
    }
    if (parser->is_done()) co_return response;  // no body
    const auto body_timeout = options_.body_timeout;
    response.stream = [stream, buffer, parser, body_timeout](chargewire::AppBodyWriter& writer) -> Task<bool> {
      std::vector<char> piece(kReadBuffer);
      while (!parser->is_done()) {
        if (writer.ClientGone()) co_return false;
        parser->get().body().data = piece.data();
        parser->get().body().size = piece.size();
        beast::get_lowest_layer(*stream).expires_after(body_timeout);
        boost::system::error_code ec;
        co_await http::async_read_some(*stream, *buffer, *parser, net::redirect_error(net::use_awaitable, ec));
        if (ec && ec != http::error::need_buffer) co_return false;
        const std::size_t got = piece.size() - parser->get().body().size;
        if (got > 0) co_await writer.Write(std::string_view(piece.data(), got));
      }
      co_return true;
    };
    co_return response;
  }

  // GET `path` at the remote; the answer's status and body (read whole, bounded).
  template <class Stream>
  Task<std::pair<int, std::string>> Get(Stream& stream, const Target& target, const std::string& path) {
    http::request<http::empty_body> request{http::verb::get, path, 11};
    request.set(http::field::host, target.host + ":" + target.port);
    request.set(http::field::user_agent, "hardcharger-web-relay/" + options_.version);
    request.set(http::field::accept, "application/json");
    beast::get_lowest_layer(stream).expires_after(options_.connect_timeout);
    co_await http::async_write(stream, request, net::use_awaitable);
    beast::flat_buffer buffer;
    http::response_parser<http::string_body> parser;
    parser.body_limit(kMaxJsonBody * 4);
    co_await http::async_read(stream, buffer, parser, net::use_awaitable);
    co_return std::pair<int, std::string>{static_cast<int>(parser.get().result_int()), parser.get().body()};
  }

  Task<void> Connect(beast::tcp_stream& stream, const Target& target) {
    tcp::resolver resolver(co_await net::this_coro::executor);
    boost::system::error_code ec;
    const auto endpoints = co_await resolver.async_resolve(target.host, target.port, net::redirect_error(net::use_awaitable, ec));
    if (ec) throw Problem(502, "remote_unreachable", target.origin + ": " + ec.message());
    stream.expires_after(options_.connect_timeout);
    co_await stream.async_connect(endpoints, net::redirect_error(net::use_awaitable, ec));
    if (ec) throw Problem(502, "remote_unreachable", "cannot connect to " + target.origin + ": " + ec.message());
    stream.socket().set_option(tcp::no_delay(true), ec);
  }

  // Connects and shakes hands; checks the certificate the way `mode` says. `describe`: read
  // the certificate's details (a probe); a forward only checks it.
  Task<CertificateInfo> Handshake(beast::ssl_stream<beast::tcp_stream>& stream, const Target& target, TlsMode mode,
                                  const std::string& fingerprint, bool describe) {
    co_await Connect(beast::get_lowest_layer(stream), target);
    boost::system::error_code ec;
    const bool literal_address = !net::ip::make_address(target.host, ec).is_unspecified() && !ec;
    if (!literal_address && !SSL_set_tlsext_host_name(stream.native_handle(), target.host.c_str())) {
      throw Problem(502, "remote_tls_failed", "cannot set the TLS server name for " + target.host);
    }
    if (mode == TlsMode::kVerified) {
      stream.set_verify_mode(ssl::verify_peer);
      stream.set_verify_callback(ssl::host_name_verification(target.host));
    } else {
      stream.set_verify_mode(ssl::verify_none);
    }
    beast::get_lowest_layer(stream).expires_after(options_.connect_timeout);
    co_await stream.async_handshake(ssl::stream_base::client, net::redirect_error(net::use_awaitable, ec));
    if (ec) {
      std::string why = ec.message();
      if (mode == TlsMode::kVerified) {
        const long result = SSL_get_verify_result(stream.native_handle());
        if (result != X509_V_OK) why = X509_verify_cert_error_string(result);
        throw Problem(502, "remote_certificate_untrusted",
                      target.origin + "'s certificate did not verify: " + why +
                          ". Trust it by its fingerprint if you know the server.");
      }
      throw Problem(502, "remote_tls_failed", "TLS to " + target.origin + " failed: " + why);
    }
    X509* cert = SSL_get1_peer_certificate(stream.native_handle());
    if (cert == nullptr) throw Problem(502, "remote_tls_failed", target.origin + " presented no certificate");
    CertificateInfo info = describe ? DescribeCertificate(cert) : CertificateInfo{};
    if (!describe) info.fingerprint = DescribeCertificate(cert).fingerprint;
    X509_free(cert);
    info.verified = mode == TlsMode::kVerified;
    if (mode == TlsMode::kFingerprint && !fingerprint.empty() && info.fingerprint != fingerprint) {
      throw Problem(502, "remote_certificate_mismatch",
                    target.origin + " presented a certificate with SHA-256 " + info.fingerprint +
                        ", not the trusted " + fingerprint + ". Remove the server and add it again to trust the new one.");
    }
    co_return info;
  }

  // One TLS context for verified remotes (the system's roots, read once) and one that
  // verifies nothing (fingerprint mode checks the certificate itself).
  std::shared_ptr<ssl::context> Context(TlsMode mode) {
    std::lock_guard lock(mutex_);
    auto& slot = mode == TlsMode::kVerified ? verified_context_ : trusting_context_;
    if (slot) return slot;
    auto context = std::make_shared<ssl::context>(ssl::context::tls_client);
    SSL_CTX_set_min_proto_version(context->native_handle(), TLS1_2_VERSION);
    if (mode == TlsMode::kVerified) {
      context->set_verify_mode(ssl::verify_peer);
      bool loaded = false;
      if (const char* env = std::getenv("SSL_CERT_FILE"); env != nullptr && *env != '\0' && std::ifstream(env).good()) {
        boost::system::error_code ec;
        context->load_verify_file(env, ec);
        loaded = !ec;
      }
      if (!loaded) {
        for (const char* bundle : {"/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                                   "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem",
                                   "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem"}) {
          if (!std::ifstream(bundle).good()) continue;
          boost::system::error_code ec;
          context->load_verify_file(bundle, ec);
          if (!ec) {
            loaded = true;
            break;
          }
        }
      }
      if (!loaded && !AddSystemTrustStore(context->native_handle())) context->set_default_verify_paths();
    } else {
      context->set_verify_mode(ssl::verify_none);
    }
    slot = context;
    return slot;
  }

  void Log(bool warning, const std::string& message, const RelayLogFields& fields) const {
    if (options_.log) options_.log(warning, message, fields);
  }

  RelayOptions options_;
  mutable std::mutex mutex_;
  std::map<std::string, std::shared_ptr<RelaySession>> sessions_;
  Clock::time_point next_sweep_{};
  std::shared_ptr<ssl::context> verified_context_;
  std::shared_ptr<ssl::context> trusting_context_;
};

Relay::Relay(RelayOptions options) : impl_(std::make_unique<Impl>(std::move(options))) {}
Relay::~Relay() = default;
std::vector<chargewire::AppEndpoint> Relay::Endpoints() { return impl_->Endpoints(); }
std::size_t Relay::SessionCount() const { return impl_->SessionCount(); }

}  // namespace hardcharger::web
