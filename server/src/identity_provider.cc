#include "identity_provider.hpp"

#include <stdexcept>

#include "logging.hpp"

namespace hardcharger {

IdentityProvider DiscoverIdentityProvider(const Config& c) {
  IdentityProvider provider;
  const bool oauth = !c.oauth_client_id.empty();
  const bool by_issuer =
      !c.token_allowed_issuer.empty() && !c.token_allowed_audience.empty() && c.token_signature_verify_cert_path.empty();
  if (oauth || (c.token_jwks_uri.empty() && by_issuer)) {
    try {
      provider.endpoints = DiscoverOidc(c.token_allowed_issuer);
    } catch (const std::exception& e) {
      // Without OAuth this only costs JWKS sign-in; OAuth can't run.
      if (oauth) throw std::runtime_error(std::string("OAuth sign-in needs the issuer's OpenID configuration: ") + e.what());
      Log(LogLevel::kWarning, "OpenID discovery failed; identity provider tokens won't be accepted",
          {{"issuer", c.token_allowed_issuer}, {"error", e.what()}});
      return provider;
    }
  }
  const std::string uri = !c.token_jwks_uri.empty() ? c.token_jwks_uri : provider.endpoints.jwks_uri;
  if (!uri.empty() && (!c.token_jwks_uri.empty() || by_issuer || oauth)) {
    provider.jwks = std::make_shared<Jwks>(uri);
    Log(LogLevel::kInfo, "identity provider tokens are verified with its JWKS", {{"jwks_uri", uri}});
  }
  return provider;
}

}  // namespace hardcharger
