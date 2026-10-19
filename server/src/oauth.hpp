// Browser sign-in through the identity provider (Warp SQL §4.3, capability
// oauth_browser), on the main port. The Warp SQL SDK serves the flows, the polls and the
// callback (GET /warp/v1/auth/oauth/callback, the redirect URI operators register with the
// provider) and issues the access token; this provider builds the authorization request
// (code flow with PKCE, state and nonce) and exchanges the code as a confidential client.
// The ID token it receives signs the user in exactly as the token_exchange grant would
// (the authenticator verifies it with the provider's JWKS, issuer, audience, authorized
// e-mails, role and store user), after its nonce is checked against the flow's. Clients
// never see the ID token, only Hardcharger's own access token.
//
// With oauth_instance_id the state starts "<instance-id>.", so a proxy that receives the
// provider's redirects for several servers can route each to its server.
#pragma once

#include <functional>
#include <string>

#include "warpsql/backend.hpp"

namespace hardcharger {

class OAuthBrowserSignIn : public warpsql::BrowserSignInProvider {
 public:
  struct Options {
    std::string client_id;
    std::string client_secret;
    std::string scopes;
    std::string redirect_uri;
    std::string instance_id;
    std::string provider_name;
    // The server's public origin (scheme://host[:port]) when oauth_base_url names one: the
    // redirect back to a web page's return_to is then absolute (a callback through a proxy).
    std::string public_origin;
    std::string authorization_endpoint;
    std::string token_endpoint;
    // Signs the user in with the provider's ID token (the authenticator's token_exchange).
    std::function<warpsql::Principal(std::string_view id_token)> sign_in;
  };

  explicit OAuthBrowserSignIn(Options options);

  std::string DisplayName() override { return options_.provider_name; }
  std::string AuthorizeUrl(const Authorization& authorization) override;
  warpsql::Principal CompleteSignIn(const Callback& callback) override;
  std::string StatePrefix() override { return options_.instance_id.empty() ? "" : options_.instance_id + "."; }
  std::string PublicOrigin() override { return options_.public_origin; }

 private:
  // The ID token for `code`, from the provider's token endpoint; throws with the reason.
  std::string ExchangeCode(const Callback& callback) const;

  const Options options_;
};

}  // namespace hardcharger
