#include "oauth.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>
#include <openssl/hmac.h>

#include <atomic>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/json.hpp>
#include <boost/url/parse.hpp>
#include <condition_variable>
#include <format>
#include <thread>

#include "http_client.hpp"
#include "logging.hpp"
#include "patterns.hpp"
#include "platform.hpp"
#include "tokens.hpp"

namespace hardcharger {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
namespace json = boost::json;
namespace ssl = asio::ssl;
using tcp = asio::ip::tcp;

namespace {

// A flow nobody finished is forgotten after this long.
constexpr auto kFlowLifetime = std::chrono::minutes(15);
// Flows waiting at once: anyone can start one, so their number is bounded.
constexpr std::size_t kMaxPending = 10000;
// A connection that sends nothing for this long is dropped.
constexpr int kReadTimeoutSeconds = 30;

std::string Escape(std::string_view text) {
  std::string out;
  for (char c : text) {
    switch (c) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += c;
    }
  }
  return out;
}

// The pages a browser lands on. The error is the provider's or ours, escaped: the
// callback's query is the visitor's to choose.
OAuthServer::Reply Page(int status, std::string_view title, std::string_view message) {
  return {status, "text/html; charset=utf-8",
          std::format("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Hardcharger - {0}</title>"
                      "<style>body{{font-family:system-ui,sans-serif;display:flex;justify-content:center;"
                      "margin-top:15vh;color:#222}}.box{{max-width:32em;text-align:center}}"
                      ".error{{color:#b00020}}</style></head><body><div class=\"box\"><h1>{0}</h1>{1}</div>"
                      "</body></html>",
                      title, message),
          {}};
}

OAuthServer::Reply Success() {
  return Page(200, "Authentication Successful",
              "<p>You have been signed in. You may close this tab.</p>"
              "<script>setTimeout(function(){window.close();},3000);</script>");
}

OAuthServer::Reply Failure(int status, std::string_view error) {
  return Page(status, "Authentication Failed",
              "<p class=\"error\">" + Escape(error) + "</p><p>Please close this tab and try again.</p>");
}

OAuthServer::Reply Expired() {
  return Page(200, "Session Expired", "<p>This sign-in has expired. Please try again.</p>");
}

OAuthServer::Reply Json(int status, const json::object& body) {
  return {status, "application/json", json::serialize(body), {}};
}

bool IsUuid(std::string_view text) {
  if (text.size() != 36) return false;
  for (std::size_t i = 0; i < text.size(); i++) {
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? text[i] != '-' : !std::isxdigit(static_cast<unsigned char>(text[i]))) return false;
  }
  return true;
}

}  // namespace

// Accepts on one thread and answers each connection on its own, so a callback waiting on
// the provider's token endpoint doesn't hold up the others.
class OAuthServer::Listener {
 public:
  Listener(OAuthServer& owner, const Options& options)
      : owner_(owner), acceptor_(io_, tcp::endpoint(asio::ip::make_address(options.host), options.port), kListenerReusesAddress) {
    if (!options.tls_cert.empty()) {
      tls_.emplace(ssl::context::tls_server);
      tls_->set_options(ssl::context::default_workarounds | ssl::context::no_sslv2 | ssl::context::no_sslv3 |
                        ssl::context::no_tlsv1 | ssl::context::no_tlsv1_1);
      tls_->use_certificate_chain_file(options.tls_cert);
      tls_->use_private_key_file(options.tls_key, ssl::context::pem);
    }
  }

  uint16_t port() const { return acceptor_.local_endpoint().port(); }

  void Start() {
    Accept();
    thread_ = std::thread([this] { io_.run(); });
  }

  void Stop() {
    io_.stop();
    if (thread_.joinable()) thread_.join();
    std::unique_lock lock(mutex_);
    idle_.wait(lock, [this] { return active_ == 0; });
  }

 private:
  void Accept() {
    acceptor_.async_accept([this](boost::system::error_code ec, tcp::socket socket) {
      if (ec) return;
      {
        std::lock_guard lock(mutex_);
        active_++;
      }
      std::thread([this, socket = std::move(socket)]() mutable {
        Serve(std::move(socket));
        std::lock_guard lock(mutex_);
        if (--active_ == 0) idle_.notify_all();
      }).detach();
      Accept();
    });
  }

  void Serve(tcp::socket socket) {
    SetSocketTimeouts(socket, kReadTimeoutSeconds);
    boost::system::error_code ec;
    if (!tls_) {
      Answer(socket, ec);
      socket.shutdown(tcp::socket::shutdown_both, ec);
      return;
    }
    ssl::stream<tcp::socket> stream(std::move(socket), *tls_);
    stream.handshake(ssl::stream_base::server, ec);
    if (ec) return;
    Answer(stream, ec);
    stream.shutdown(ec);
  }

  template <class Stream>
  void Answer(Stream& stream, boost::system::error_code& ec) {
    beast::flat_buffer buffer;
    http::request<http::string_body> request;
    http::read(stream, buffer, request, ec);
    if (ec) return;
    Reply reply = request.method() == http::verb::get
                      ? owner_.Handle(std::string(request.target()))
                      : Reply{405, "application/json", R"({"status":"method_not_allowed"})", {}};
    http::response<http::string_body> response(static_cast<http::status>(reply.status), request.version());
    response.keep_alive(false);
    response.set(http::field::cache_control, "no-store");
    if (!reply.content_type.empty()) response.set(http::field::content_type, reply.content_type);
    if (!reply.location.empty()) response.set(http::field::location, reply.location);
    response.body() = std::move(reply.body);
    response.prepare_payload();
    http::write(stream, response, ec);
  }

  OAuthServer& owner_;
  asio::io_context io_;
  tcp::acceptor acceptor_;
  std::optional<ssl::context> tls_;
  std::thread thread_;
  std::mutex mutex_;
  std::condition_variable idle_;
  int active_ = 0;
};

std::string OAuthServer::Hmac(const std::string& key, const std::string& data) {
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int length = 0;
  HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()), reinterpret_cast<const unsigned char*>(data.data()),
       data.size(), digest, &length);
  std::string out;
  for (unsigned int i = 0; i < length; i++) out += std::format("{:02x}", digest[i]);
  return out;
}

OAuthServer::OAuthServer(Options options) : options_(std::move(options)) {
  listener_ = std::make_unique<Listener>(*this, options_);
}

OAuthServer::~OAuthServer() { Stop(); }

void OAuthServer::Start() {
  listener_->Start();
  Log(LogLevel::kInfo, "OAuth sign-in on", {{"port", std::to_string(port())}, {"redirect_uri", options_.redirect_uri}});
}

void OAuthServer::Stop() {
  if (listener_) listener_->Stop();
}

uint16_t OAuthServer::port() const { return listener_->port(); }

OAuthServer::Reply OAuthServer::Handle(const std::string& target) {
  Expire();
  auto parsed = boost::urls::parse_origin_form(target);
  if (!parsed) return Json(400, {{"status", "bad_request"}});
  std::map<std::string, std::string> query;
  for (const auto& param : parsed->params()) query[std::string(param.key)] = std::string(param.value);
  const std::string path(parsed->path());
  if (path == "/oauth/initiate") return Initiate();
  if (path == "/oauth/start") return StartFlow(query);
  if (path == "/oauth/callback") return Callback(query);
  if (path.starts_with("/oauth/token/") && IsUuid(path.substr(13))) return Poll(path.substr(13));
  return Json(404, {{"status", "not_found"}});
}

std::string OAuthServer::AuthorizeUrl(const std::string& hash) const {
  const std::string state = options_.instance_id.empty() ? hash : options_.instance_id + "." + hash;
  const char separator = options_.authorization_endpoint.find('?') == std::string::npos ? '?' : '&';
  return options_.authorization_endpoint + separator + "response_type=code&client_id=" +
         UrlEncode(options_.client_id) + "&redirect_uri=" + UrlEncode(options_.redirect_uri) +
         "&scope=" + UrlEncode(options_.scopes) + "&state=" + UrlEncode(state);
}

OAuthServer::Reply OAuthServer::Initiate() {
  const std::string uuid = RandomUuid();
  const std::string hash = Hmac(options_.secret_key, uuid);
  {
    std::lock_guard lock(mutex_);
    if (pending_.size() >= kMaxPending) return Json(503, {{"status", "busy"}});
    pending_[hash] = {std::chrono::steady_clock::now(), std::nullopt, std::nullopt};
  }
  return Json(200, {{"session_uuid", uuid}, {"auth_url", AuthorizeUrl(hash)}});
}

OAuthServer::Reply OAuthServer::StartFlow(const std::map<std::string, std::string>& query) {
  auto it = query.find("session");
  if (it == query.end() || it->second.empty()) return Failure(400, "Missing session parameter");
  {
    std::lock_guard lock(mutex_);
    if (pending_.contains(it->second)) return Failure(409, "Session already in progress");
    if (pending_.size() >= kMaxPending) return Failure(503, "Too many sign-ins in progress; try again shortly");
    pending_[it->second] = {std::chrono::steady_clock::now(), std::nullopt, std::nullopt};
  }
  Reply reply{302, {}, {}, AuthorizeUrl(it->second)};
  return reply;
}

void OAuthServer::Fail(const std::string& hash, const std::string& error) {
  std::lock_guard lock(mutex_);
  if (auto it = pending_.find(hash); it != pending_.end()) it->second.error = error;
}

OAuthServer::Reply OAuthServer::Callback(const std::map<std::string, std::string>& query) {
  auto state = query.find("state");
  std::string hash = state == query.end() ? std::string() : state->second;
  if (const std::string prefix = options_.instance_id + "."; !options_.instance_id.empty() &&
                                                            hash.size() > prefix.size() && hash.starts_with(prefix)) {
    hash = hash.substr(prefix.size());
  }
  if (auto error = query.find("error"); error != query.end()) {
    auto description = query.find("error_description");
    const std::string reason = description != query.end() ? description->second : error->second;
    Log(LogLevel::kWarning, "OAuth sign-in refused by the identity provider", {{"error", reason}});
    Fail(hash, reason);
    return Failure(200, reason);
  }
  auto code = query.find("code");
  if (code == query.end() || state == query.end()) return Failure(400, "Missing code or state parameter");
  {
    std::lock_guard lock(mutex_);
    auto it = pending_.find(hash);
    if (it == pending_.end()) return Expired();
    if (it->second.id_token || it->second.error) return Failure(409, "Session already completed");
  }
  std::string id_token;
  try {
    id_token = ExchangeCode(code->second);
    // Who it names, checked now so the browser says so; the signature is checked when the
    // client signs in with the token.
    auto decoded = jwt::decode<jwt::traits::boost_json>(id_token);
    const std::string user = decoded.has_payload_claim("email") ? decoded.get_payload_claim("email").as_string()
                                                                : (decoded.has_subject() ? decoded.get_subject() : "");
    if (user.empty() || !MatchesAnyIgnoringCase(options_.authorized_emails, user)) {
      throw std::runtime_error("User '" + user + "' is not authorized. Contact your administrator.");
    }
    Log(LogLevel::kInfo, "OAuth sign-in completed", {{"user", user}, {"method", "oauth"}});
  } catch (const std::exception& e) {
    Log(LogLevel::kWarning, "OAuth sign-in failed", {{"error", e.what()}});
    Fail(hash, e.what());
    return Failure(200, e.what());
  }
  std::lock_guard lock(mutex_);
  if (auto it = pending_.find(hash); it != pending_.end()) it->second.id_token = std::move(id_token);
  return Success();
}

OAuthServer::Reply OAuthServer::Poll(const std::string& uuid) {
  const std::string hash = Hmac(options_.secret_key, uuid);
  std::lock_guard lock(mutex_);
  auto it = pending_.find(hash);
  if (it == pending_.end()) return Json(404, {{"status", "not_found"}});
  if (it->second.error) return Json(200, {{"status", "error"}, {"error", *it->second.error}});
  if (it->second.id_token) {
    // Handed out once.
    json::object body{{"status", "complete"}, {"token", *it->second.id_token}};
    pending_.erase(it);
    return Json(200, body);
  }
  return Json(200, {{"status", "pending"}});
}

std::string OAuthServer::ExchangeCode(const std::string& code) const {
  const HttpResponse response = HttpPostForm(
      options_.token_endpoint, {{"grant_type", "authorization_code"},
                                {"code", code},
                                {"redirect_uri", options_.redirect_uri},
                                {"client_id", options_.client_id},
                                {"client_secret", options_.client_secret}});
  boost::system::error_code ec;
  json::value body = json::parse(response.body, ec);
  const json::object* object = !ec && body.is_object() ? &body.as_object() : nullptr;
  if (const auto* refused = object != nullptr ? object->if_contains("error") : nullptr; refused != nullptr) {
    std::string error = refused->is_string() ? std::string(refused->as_string()) : json::serialize(*refused);
    if (const auto* description = object->if_contains("error_description"); description && description->is_string()) {
      error += ": " + std::string(description->as_string());
    }
    throw std::runtime_error("the identity provider refused the code: " + error);
  }
  if (response.status != 200) {
    throw std::runtime_error("the identity provider's token endpoint returned HTTP " + std::to_string(response.status));
  }
  const auto* id_token = object != nullptr ? object->if_contains("id_token") : nullptr;
  if (id_token == nullptr || !id_token->is_string()) {
    throw std::runtime_error("the identity provider's answer has no id_token");
  }
  return std::string(id_token->as_string());
}

void OAuthServer::Expire() {
  const auto now = std::chrono::steady_clock::now();
  std::lock_guard lock(mutex_);
  std::erase_if(pending_, [&](const auto& item) { return now - item.second.created > kFlowLifetime; });
}

}  // namespace hardcharger
