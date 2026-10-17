#include "jwks.hpp"

#include <jwt-cpp/jwt.h>

#include <boost/json.hpp>
#include <stdexcept>

#include "http_client.hpp"
#include "logging.hpp"

namespace hardcharger {

namespace json = boost::json;

namespace {

// Another fetch within this time reuses the last one: a burst of tokens naming an
// unknown kid must not become a burst of requests to the provider.
constexpr auto kMinRefetch = std::chrono::seconds(5);

json::object Document(const std::string& url) {
  const HttpResponse response = HttpGet(url);
  if (response.status != 200) {
    throw std::runtime_error("GET " + url + " returned HTTP " + std::to_string(response.status));
  }
  boost::system::error_code ec;
  json::value value = json::parse(response.body, ec);
  if (ec || !value.is_object()) throw std::runtime_error(url + " did not return a JSON object");
  return value.as_object();
}

std::string Text(const json::object& object, std::string_view key) {
  const auto* value = object.if_contains(key);
  return value != nullptr && value->is_string() ? std::string(value->as_string()) : std::string();
}

}  // namespace

OidcEndpoints DiscoverOidc(const std::string& issuer) {
  std::string base = issuer;
  while (!base.empty() && base.back() == '/') base.pop_back();
  const std::string url = base + "/.well-known/openid-configuration";
  const json::object document = Document(url);
  OidcEndpoints endpoints{Text(document, "jwks_uri"), Text(document, "authorization_endpoint"),
                          Text(document, "token_endpoint")};
  if (endpoints.jwks_uri.empty()) throw std::runtime_error("the OpenID configuration at " + url + " has no jwks_uri");
  return endpoints;
}

Jwks::Jwks(std::string uri, std::chrono::minutes ttl) : uri_(std::move(uri)), ttl_(ttl) {}

std::unordered_map<std::string, Jwks::Key> Jwks::Parse(const std::string& document) {
  boost::system::error_code ec;
  json::value value = json::parse(document, ec);
  const auto* keys = !ec && value.is_object() ? value.as_object().if_contains("keys") : nullptr;
  if (keys == nullptr || !keys->is_array()) throw std::runtime_error("the JWKS has no 'keys' array");
  std::unordered_map<std::string, Key> out;
  for (const auto& entry : keys->as_array()) {
    if (!entry.is_object()) continue;
    const auto& jwk = entry.as_object();
    const std::string kid = Text(jwk, "kid");
    const std::string type = Text(jwk, "kty");
    if (kid.empty()) continue;
    try {
      if (type == "RSA" && !Text(jwk, "n").empty() && !Text(jwk, "e").empty()) {
        out[kid] = {jwt::helper::create_public_key_from_rsa_components(Text(jwk, "n"), Text(jwk, "e")),
                    Text(jwk, "alg")};
      } else if (type == "EC" && !Text(jwk, "crv").empty()) {
        out[kid] = {jwt::helper::create_public_key_from_ec_components(Text(jwk, "crv"), Text(jwk, "x"), Text(jwk, "y")),
                    Text(jwk, "alg")};
      }
    } catch (const std::exception& e) {
      Log(LogLevel::kWarning, "JWKS key skipped", {{"kid", kid}, {"error", e.what()}});
    }
  }
  if (out.empty()) throw std::runtime_error("the JWKS has no usable RSA or EC key");
  return out;
}

Jwks::Key Jwks::Find(const std::string& kid) {
  std::lock_guard lock(mutex_);
  const auto now = std::chrono::steady_clock::now();
  const bool fresh = fetched_ && now - fetched_at_ < ttl_;
  if (auto it = keys_.find(kid); fresh && it != keys_.end()) return it->second;
  if (!fetched_ || now - fetched_at_ >= kMinRefetch) {
    const HttpResponse response = HttpGet(uri_);
    if (response.status != 200) {
      throw std::runtime_error("GET " + uri_ + " returned HTTP " + std::to_string(response.status));
    }
    keys_ = Parse(response.body);
    fetched_at_ = now;
    fetched_ = true;
    Log(LogLevel::kDebug, "JWKS fetched", {{"uri", uri_}, {"keys", std::to_string(keys_.size())}});
  }
  if (auto it = keys_.find(kid); it != keys_.end()) return it->second;
  throw std::runtime_error("no key '" + kid + "' in the JWKS at " + uri_);
}

}  // namespace hardcharger
