// How users sign in to Hardcharger (spec §4.1):
//  - password: the configured administrator;
//  - token_exchange: a signed sign-in token (RS256) from a trusted issuer, such as the
//    tokens GizmoData Cloud mints for its users. As in GizmoSQL 1.x, the same token is
//    also accepted as the password of the user named "token".
//  - with access control on, also the store's users with a password, and the scoped
//    tokens CREATE SCOPED TOKEN hands out (as the password of user "token" too).
#pragma once

#include <functional>
#include <memory>
#include <string>

#include "authz/authz.hpp"
#include "config.hpp"
#include "license.hpp"
#include "warpsql/backend.hpp"

namespace hardcharger {

// Who a principal is to access control, from the claims its access token carries.
authz::Identity IdentityOf(const warpsql::Principal& principal);

class Authenticator : public warpsql::Authenticator {
 public:
  // `access`: access control, or null. `server_tokens` validates this server's own
  // access tokens (for scoped tokens sent as a password).
  Authenticator(const Config& config, const License& license,
                std::shared_ptr<authz::AccessControl> access = nullptr,
                std::function<warpsql::Principal(std::string_view)> server_tokens = nullptr);

  warpsql::Principal AuthenticatePassword(std::string_view username,
                                          std::string_view password) override;
  warpsql::Principal ExchangeToken(std::string_view subject_token) override;
  boost::json::array GrantTypes() override;

 private:
  std::string username_;
  std::string password_;
  std::string verify_key_pem_;  // empty: sign-in tokens are not accepted
  std::string allowed_issuer_;
  std::string allowed_audience_;
  std::string default_role_;
  std::string groups_claim_;
  const License& license_;
  std::shared_ptr<authz::AccessControl> access_;
  std::function<warpsql::Principal(std::string_view)> server_tokens_;
};

}  // namespace hardcharger
