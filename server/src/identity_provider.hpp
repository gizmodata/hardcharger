// The identity provider whose sign-in tokens the server accepts, from the configuration:
// the keys that verify them (a configured key, or the provider's JWKS, found directly or
// through the issuer's OpenID configuration) and, for browser sign-in, the provider's
// endpoints. Shared by the server and --verify-credentials, so both decide a token alike.
#pragma once

#include <memory>

#include "config.hpp"
#include "jwks.hpp"

namespace hardcharger {

struct IdentityProvider {
  std::shared_ptr<Jwks> jwks;  // the provider's keys, or null (the configured key only)
  OidcEndpoints endpoints;
};

// OpenID discovery runs when OAuth is on, or when tokens are verified by issuer (an issuer
// and audience, no configured key) without a jwks_uri. Throws std::runtime_error when OAuth
// is on and discovery fails; otherwise logs it and returns a provider without keys.
IdentityProvider DiscoverIdentityProvider(const Config& config);

}  // namespace hardcharger
