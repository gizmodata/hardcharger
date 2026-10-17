// Browser sign-in through the server's OAuth sign-in server (hardcharger-server's
// --oauth-port routes, as in GizmoSQL 1.x): GET /oauth/initiate, open its auth_url in a
// browser, poll /oauth/token/{uuid} until the identity provider has answered, then sign
// in to Warp SQL with the ID token (grant token_exchange).
#pragma once

#include <string>

#include <arrow/result.h>

#include "warpsql/client.hpp"

namespace hardcharger::client {

class OAuthFlow {
 public:
  explicit OAuthFlow(warpsql::client::TlsOptions tls) : tls_(std::move(tls)) {}

  // The identity provider's ID token, once the user has signed in in the browser.
  arrow::Result<std::string> Authenticate(const std::string& oauth_base_url);

 private:
  warpsql::client::TlsOptions tls_;
};

}  // namespace hardcharger::client
