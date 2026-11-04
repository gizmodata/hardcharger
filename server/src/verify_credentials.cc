#include "verify_credentials.hpp"

#include <boost/json.hpp>

#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>

#include "auth.hpp"
#include "authz/authz.hpp"
#include "authz/store.hpp"
#include "identity_provider.hpp"
#include "logging.hpp"
#include "tokens.hpp"
#include "chargewire/errors.hpp"

namespace hardcharger {

namespace {

constexpr size_t kMaxInputBytes = 64 * 1024;  // a credential, not a document

int Answer(std::ostream& out, int exit_code, boost::json::object answer) {
  out << boost::json::serialize(answer) << "\n" << std::flush;
  return exit_code;
}

int Refused(std::ostream& out, std::string reason, std::string message) {
  return Answer(out, kRefusedExit,
                {{"verified", false}, {"reason", std::move(reason)}, {"message", std::move(message)}});
}

int Undecided(std::ostream& out, std::string reason, std::string message) {
  return Answer(out, kUndecidedExit,
                {{"verified", false}, {"reason", std::move(reason)}, {"message", std::move(message)}});
}

int BadInput(std::ostream& out, std::string message) {
  return Answer(out, kBadInputExit, {{"verified", false}, {"reason", "bad_input"}, {"message", std::move(message)}});
}

// What kept the store from answering the question: its message, classified for the caller.
std::string StoreProblem(const std::string& message) {
  if (message.find(authz::kStoreUnavailable) != std::string::npos) return "store_unavailable";
  if (message.find("has not been initialized") != std::string::npos) return "store_uninitialized";
  if (message.find("belongs to cluster") != std::string::npos) return "store_belongs_to_another_cluster";
  return "configuration";
}

std::string StringField(const boost::json::object& object, const char* name) {
  const auto* value = object.if_contains(name);
  if (value == nullptr || !value->is_string()) return {};
  return std::string(value->as_string());
}

}  // namespace

int VerifyCredentials(const Config& config, std::istream& in, std::ostream& out) {
  // stdout carries the one answer; everything else goes to stderr, whatever the
  // configuration says (a log file would hide a Job's output).
  ConfigureLogging(config.log_level, config.log_format, "", config.log_emoji);
  SetDefaultLogFields({{"action", "verify-credentials"}});

  std::string text;
  {
    char buffer[4096];
    while (in.read(buffer, sizeof buffer) || in.gcount() > 0) {
      text.append(buffer, static_cast<size_t>(in.gcount()));
      if (text.size() > kMaxInputBytes) return BadInput(out, "the credential on stdin is larger than 64 KiB");
    }
  }
  boost::json::object request;
  try {
    auto parsed = boost::json::parse(text);
    if (!parsed.is_object()) return BadInput(out, "stdin must hold one JSON object");
    request = std::move(parsed.as_object());
  } catch (const std::exception& e) {
    // boost.json's message carries its source location; the first clause says enough.
    std::string why = e.what();
    if (const auto at = why.find(" ["); at != std::string::npos) why.resize(at);
    return BadInput(out, "stdin is not JSON: " + why);
  }
  const std::string grant_type = StringField(request, "grant_type");
  if (grant_type.empty()) return BadInput(out, "grant_type is required (password or token_exchange)");
  if (grant_type == "client_certificate") {
    return Undecided(out, "unsupported_grant",
                     "a client certificate is verified by the TLS connection; it cannot be checked here");
  }
  if (grant_type != "password" && grant_type != "token_exchange") {
    return BadInput(out, "unknown grant_type '" + grant_type + "' (password or token_exchange)");
  }
  const std::string username = StringField(request, "username");
  const std::string password = StringField(request, "password");
  const std::string subject_token = StringField(request, "subject_token");
  if (grant_type == "password" && (username.empty() || password.empty())) {
    return BadInput(out, "a password grant needs username and password");
  }
  if (grant_type == "token_exchange" && subject_token.empty()) {
    return BadInput(out, "a token_exchange grant needs subject_token");
  }

  // The store, read-only: it decides store users, scoped tokens and provisioning exactly as
  // the server's does, and is never claimed, created or changed.
  std::shared_ptr<authz::AccessControl> access;
  if (!config.rbac_store_uri.empty()) {
    authz::Options options;
    options.store_uri = config.rbac_store_uri;
    options.administrator_name = config.admin_username;
    options.certificate_administrators = config.mtls_admin_identities;
    options.store_timeout = std::chrono::seconds(config.rbac_store_timeout_seconds);
    options.cluster_id = config.cluster_id;
    options.store_shared = config.rbac_store_shared;
    options.read_only = true;
    // Not asked: the policy service decides statements, not sign-ins.
    try {
      access = std::make_shared<authz::AccessControl>(options);
    } catch (const std::exception& e) {
      return Undecided(out, StoreProblem(e.what()), e.what());
    }
    Log(LogLevel::kInfo, "checking against the access control store, read-only",
        {{"store", access->store_description()}});
  }
  // A server without a secret key signs tokens with a key of its own that dies with it, so
  // no earlier token is good there; this check answers the same way.
  const std::string secret_key = config.secret_key.empty() ? RandomSecretKey() : config.secret_key;
  if (config.secret_key.empty() && access) {
    Log(LogLevel::kWarning, "no secret_key: no scoped token can be verified (none survives a restart of the server)");
  }
  auto tokens = std::make_shared<JwtTokenService>(secret_key, std::chrono::seconds(config.token_lifetime_seconds),
                                                  RandomUuid(), config.allow_cross_instance_tokens,
                                                  ParseLogLevel(config.auth_log_level), access);
  try {
    const IdentityProvider provider = DiscoverIdentityProvider(config);
    Authenticator authenticator(config, access, [tokens](std::string_view token) { return tokens->Validate(token); },
                                provider.jwks);
    chargewire::Principal principal = grant_type == "password"
                                          ? authenticator.AuthenticatePassword(username, password)
                                          : authenticator.ExchangeToken(subject_token);
    const bool scoped = principal.claims.contains("scope_id");
    boost::json::object answer{{"verified", true},
                               {"user", principal.name},
                               {"role", principal.role},
                               {"method", scoped ? std::string("scoped_token") : principal.auth_method},
                               {"would_provision", access ? access->would_provision() : false}};
    if (access) access->Shutdown();
    return Answer(out, kVerifiedExit, std::move(answer));
  } catch (const chargewire::Error& e) {
    if (access) access->Shutdown();
    // The server's own refusal, with the reason it would give the client; a store that
    // stopped answering in between (permission_denied from a stale snapshot) is no answer.
    if (e.code() == chargewire::ErrorCode::kInvalidCredentials || e.code() == chargewire::ErrorCode::kInvalidToken) {
      return Refused(out, e.reason().empty() ? std::string(chargewire::CodeName(e.code())) : e.reason(), e.what());
    }
    return Undecided(out, StoreProblem(e.what()) == "configuration" ? std::string(chargewire::CodeName(e.code()))
                                                                    : StoreProblem(e.what()),
                     e.what());
  } catch (const std::exception& e) {
    if (access) access->Shutdown();
    return Undecided(out, StoreProblem(e.what()), e.what());
  }
}

}  // namespace hardcharger
