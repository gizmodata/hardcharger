// How users sign in to Hardcharger (spec §4.1):
//  - password: the configured administrator;
//  - token_exchange: a signed sign-in token from a trusted issuer: RS256 against a
//    configured key (the tokens GizmoData Cloud mints for its users), or an identity
//    provider's ID token verified with its JWKS. The same token is also accepted as
//    the password of the user "token", for clients that only send a password.
//  - with access control on, also the store's users with a password, and the scoped
//    tokens CREATE SCOPED TOKEN hands out (as the password of user "token" too);
//  - client_certificate (mutual TLS): the connection's verified client certificate. Its
//    identity (mtls_identity_source) signs in as the administrator when it is one of
//    mtls_admin_identities, else, with access control on, as the store user of that name.
//    The certificate proves who the user is; grants decide what they may do.
#pragma once

#include <functional>
#include <memory>
#include <string>

#include "authz/authz.hpp"
#include "config.hpp"
#include "jwks.hpp"
#include "logging.hpp"
#include "warpsql/backend.hpp"

namespace hardcharger {

// Who a principal is to access control, from the claims its access token carries.
authz::Identity IdentityOf(const warpsql::Principal& principal);

class Authenticator : public warpsql::Authenticator {
 public:
  // `access`: access control, or null. `server_tokens` validates this server's own
  // access tokens (for scoped tokens sent as a password). `jwks`: the identity
  // provider's keys, or null to verify with the configured key only.
  explicit Authenticator(const Config& config,
                std::shared_ptr<authz::AccessControl> access = nullptr,
                std::function<warpsql::Principal(std::string_view)> server_tokens = nullptr,
                std::shared_ptr<Jwks> jwks = nullptr);

  warpsql::Principal AuthenticatePassword(std::string_view username,
                                          std::string_view password) override;
  warpsql::Principal ExchangeToken(std::string_view subject_token) override;
  warpsql::Principal AuthenticateCertificate(std::string_view identity,
                                             const warpsql::ClientCertificate& certificate) override;
  boost::json::array GrantTypes() override;

 private:
  bool AcceptsTokens() const { return !verify_key_pem_.empty() || jwks_ != nullptr; }
  // Adds the key that verifies `decoded` to `verifier`; throws when there is none.
  template <class Decoded, class Verifier>
  void AllowSigningKey(const Decoded& decoded, Verifier& verifier);

  std::string username_;
  std::string password_;
  std::shared_ptr<Jwks> jwks_;  // the provider's keys, or null
  std::string verify_key_pem_;  // the configured key, or empty
  std::string allowed_issuer_;
  std::string allowed_audience_;
  std::string default_role_;
  std::string groups_claim_;
  std::vector<std::string> authorized_emails_;
  LogLevel log_level_;
  std::shared_ptr<authz::AccessControl> access_;
  std::function<warpsql::Principal(std::string_view)> server_tokens_;
  bool mutual_tls_ = false;  // client certificates are verified (mtls_ca_cert)
  std::vector<std::string> certificate_admins_;
};

}  // namespace hardcharger
