// How users sign in to Hardcharger (spec §4.1):
//  - password: the configured administrator;
//  - token_exchange: a signed sign-in token (RS256) from a trusted issuer, such as the
//    tokens GizmoData Cloud mints for its users. As in GizmoSQL 1.x, the same token is
//    also accepted as the password of the user named "token".
#pragma once

#include <string>

#include "config.hpp"
#include "license.hpp"
#include "warpsql/backend.hpp"

namespace hardcharger {

class Authenticator : public warpsql::Authenticator {
 public:
  Authenticator(const Config& config, const License& license);

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
  const License& license_;
};

}  // namespace hardcharger
