// Browser sign-in through an identity provider, on its own port (oauth_port, default
// 31339), with the routes drivers and GizmoData Cloud's shared callback proxy use:
//
//   GET /oauth/initiate           -> {"session_uuid", "auth_url"}: the client opens auth_url
//   GET /oauth/start?session=H    -> 302 to the provider, for a client that made its own
//                                    H = HMAC-SHA256(secret_key, uuid)
//   GET /oauth/callback?code&state <- the provider's redirect: the server exchanges the
//                                    code for an ID token (a confidential client)
//   GET /oauth/token/<uuid>       -> {"status": "pending" | "complete" | "error" | "not_found"};
//                                    "complete" carries the ID token, once
//
// The client then signs in with that ID token (token_exchange, or as the password of
// user "token"), which the authenticator verifies with the provider's JWKS. With
// oauth_instance_id the state is "<instance-id>.<H>", so a shared proxy can route the
// callback to the right server.
#pragma once

#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace hardcharger {

class OAuthServer {
 public:
  struct Options {
    std::string host;
    uint16_t port = 0;
    std::string client_id;
    std::string client_secret;
    std::string scopes;
    std::string redirect_uri;
    std::string secret_key;  // keys the session hashes
    std::string instance_id;
    std::vector<std::string> authorized_emails;  // globs, case-insensitive
    std::string tls_cert;  // empty: plain HTTP
    std::string tls_key;
    std::string authorization_endpoint;
    std::string token_endpoint;
  };

  explicit OAuthServer(Options options);
  ~OAuthServer();

  void Start();
  void Stop();
  uint16_t port() const;

  struct Reply {
    int status = 200;
    std::string content_type;
    std::string body;
    std::string location;
  };
  // Answers one request (`target`: path and query); public for tests.
  Reply Handle(const std::string& target);

  // HMAC-SHA256(key, data) in lower-case hex: the session hash.
  static std::string Hmac(const std::string& key, const std::string& data);

 private:
  struct Pending {
    std::chrono::steady_clock::time_point created;
    std::optional<std::string> id_token;
    std::optional<std::string> error;
  };

  Reply Initiate();
  Reply StartFlow(const std::map<std::string, std::string>& query);
  Reply Callback(const std::map<std::string, std::string>& query);
  Reply Poll(const std::string& uuid);
  std::string AuthorizeUrl(const std::string& hash) const;
  // The ID token for `code`, from the provider's token endpoint; throws with the reason.
  std::string ExchangeCode(const std::string& code) const;
  void Fail(const std::string& hash, const std::string& error);
  void Expire();

  const Options options_;
  std::mutex mutex_;
  std::map<std::string, Pending> pending_;  // by session hash

  class Listener;
  std::unique_ptr<Listener> listener_;
};

}  // namespace hardcharger
