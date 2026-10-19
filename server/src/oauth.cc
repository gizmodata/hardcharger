#include "oauth.hpp"

#include <jwt-cpp/jwt.h>
#include <jwt-cpp/traits/boost-json/traits.h>

#include <boost/json.hpp>
#include <stdexcept>

#include "http_client.hpp"
#include "logging.hpp"
#include "warpsql/errors.hpp"

namespace hardcharger {

namespace json = boost::json;

OAuthBrowserSignIn::OAuthBrowserSignIn(Options options) : options_(std::move(options)) {}

std::string OAuthBrowserSignIn::AuthorizeUrl(const Authorization& authorization) {
  const char separator = options_.authorization_endpoint.find('?') == std::string::npos ? '?' : '&';
  return options_.authorization_endpoint + separator + "response_type=code&client_id=" +
         UrlEncode(options_.client_id) + "&redirect_uri=" + UrlEncode(options_.redirect_uri) +
         "&scope=" + UrlEncode(options_.scopes) + "&state=" + UrlEncode(authorization.state) +
         "&code_challenge=" + UrlEncode(authorization.code_challenge) +
         "&code_challenge_method=S256&nonce=" + UrlEncode(authorization.nonce);
}

std::string OAuthBrowserSignIn::ExchangeCode(const Callback& callback) const {
  const HttpResponse response = HttpPostForm(
      options_.token_endpoint, {{"grant_type", "authorization_code"},
                                {"code", callback.code},
                                {"redirect_uri", options_.redirect_uri},
                                {"client_id", options_.client_id},
                                {"client_secret", options_.client_secret},
                                {"code_verifier", callback.code_verifier}});
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

warpsql::Principal OAuthBrowserSignIn::CompleteSignIn(const Callback& callback) {
  std::string id_token;
  try {
    id_token = ExchangeCode(callback);
  } catch (const std::exception& e) {
    Log(LogLevel::kWarning, "browser sign-in failed", {{"method", "oauth_browser"}, {"error", e.what()}});
    throw;
  }
  // The flow's own ID token: its nonce is the one this flow sent, so an ID token from
  // another sign-in can't complete this one. (Its signature, issuer, audience and expiry
  // are checked by the sign-in below.)
  std::string nonce;
  try {
    const auto decoded = jwt::decode<jwt::traits::boost_json>(id_token);
    if (decoded.has_payload_claim("nonce")) nonce = decoded.get_payload_claim("nonce").as_string();
  } catch (const std::exception&) {
    throw warpsql::Error(warpsql::ErrorCode::kInvalidCredentials, "the identity provider's ID token is malformed");
  }
  if (nonce != callback.nonce) {
    Log(LogLevel::kWarning, "browser sign-in refused: the ID token's nonce is not this sign-in's",
        {{"method", "oauth_browser"}});
    throw warpsql::Error(warpsql::ErrorCode::kInvalidCredentials, "the ID token's nonce is not this sign-in's");
  }
  warpsql::Principal principal = options_.sign_in(id_token);
  Log(LogLevel::kInfo, "browser sign-in completed", {{"user", principal.name}, {"method", "oauth_browser"}});
  return principal;
}

}  // namespace hardcharger
