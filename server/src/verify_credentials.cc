#include "verify_credentials.hpp"

#include <boost/json.hpp>

#include <algorithm>
#include <chrono>
#include <thread>
#include <vector>

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
#include "login_guard.hpp"
#include "tokens.hpp"
#include "chargewire/errors.hpp"

namespace hardcharger {

namespace {

constexpr size_t kMaxInputBytes = 64 * 1024;  // a credential, not a document
// A lock row older than this is a check that died holding it: the next one takes it over.
constexpr int64_t kStaleLoginLockMs = 60'000;

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
  const std::string client_address = StringField(request, "client_address");
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
    // ... but a password check counts toward the login guard's limits, as on the server.
    options.record_sign_ins = true;
    // The password lifetimes, so the answer is the sign-in's (held, or a temporary one refused).
    options.password_min_length = config.password_min_length;
    options.password_min_character_classes = config.password_min_character_classes;
    options.password_max_age_days = config.password_max_age_days;
    options.password_temporary_max_age_hours = config.password_temporary_max_age_hours;
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
  // The server's login guard, from the store's records (memory only without a store).
  LoginGuardSettings guard_settings = LoginGuardSettingsFrom(config);
  guard_settings.record_routine_successes = false;  // a success is written only when it ends failures
  const std::string origin = "verify-credentials-" + RandomUuid();
  auto guard = std::make_shared<LoginGuard>(guard_settings, origin, StoreLoginEvents(access));
  // Checks that overlap (separate processes) take turns: a lock row in the store per user and
  // per client address, taken before the records are read and given back once this check's
  // own record is written, so each sees every failure recorded before it and no more than
  // the limits allow are ever checked. (A DuckDB file is opened for writing, which admits one
  // check at a time by itself.)
  std::vector<std::string> held;
  auto finish = [&] {
    for (auto it = held.rbegin(); it != held.rend(); ++it) {
      if (auto released = access->ReleaseLoginLock(*it, origin); !released.ok()) {
        Log(LogLevel::kWarning, "a lock of this check's sign-in records could not be given back (it lapses)",
            {{"error", released.message()}});
      }
    }
    held.clear();
    if (access) access->Shutdown();
  };
  if (access && grant_type == "password") {
    std::vector<std::string> keys{"lock:user:" + LoginGuard::UserKey(username)};
    if (const std::string address = LoginGuard::AddressKey(client_address); !address.empty()) {
      keys.push_back("lock:address:" + address);
    }
    const auto give_up = std::chrono::steady_clock::now() + std::chrono::seconds(config.rbac_store_timeout_seconds);
    for (const auto& key : keys) {
      for (int attempt = 0;; ++attempt) {
        const int64_t now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                   std::chrono::system_clock::now().time_since_epoch())
                                   .count();
        auto taken = access->TakeLoginLock(key, origin, now_ms, now_ms - kStaleLoginLockMs);
        if (!taken.ok()) {
          finish();
          return Undecided(out, StoreProblem(taken.status().message()),
                           "the store's sign-in records can't be written: " + taken.status().message());
        }
        if (*taken) {
          held.push_back(key);
          break;
        }
        if (std::chrono::steady_clock::now() >= give_up) {
          finish();
          return Undecided(out, "store_busy",
                           "other checks of this user or client address kept the store's sign-in records for longer "
                           "than rbac_store_timeout_seconds");
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(std::min(20 * (attempt + 1), 200)));
      }
    }
  }
  if (std::string problem = guard->Load(); !problem.empty()) {
    finish();
    return Undecided(out, StoreProblem(problem), "the store's sign-in records can't be read: " + problem);
  }
  // Writes this check's record (a failure, or a success that ends one) before the answer;
  // returns why it could not, or empty.
  auto record = [&] {
    guard->Sync();
    std::string problem = guard->PendingProblem();
    if (!problem.empty()) {
      Log(LogLevel::kWarning, "the sign-in record of this check could not be written to the store",
          {{"error", problem}});
    }
    return problem;
  };
  try {
    const IdentityProvider provider = DiscoverIdentityProvider(config);
    Authenticator authenticator(config, access, [tokens](std::string_view token) { return tokens->Validate(token); },
                                provider.jwks, guard);
    chargewire::SignInContext context;
    context.client_address = client_address;
    chargewire::Principal principal = grant_type == "password"
                                          ? authenticator.AuthenticatePassword(username, password, context)
                                          : authenticator.ExchangeToken(subject_token);
    record();
    const bool scoped = principal.claims.contains("scope_id");
    boost::json::object answer{{"verified", true},
                               {"user", principal.name},
                               {"role", principal.role},
                               {"method", scoped ? std::string("scoped_token") : principal.auth_method},
                               {"would_provision", access ? access->would_provision() : false}};
    // A temporary password (one someone else set) is a valid credential: the cluster may wake
    // for it, and the sessions it opens run nothing but the change of that password.
    // (Said only when it is, so the answer for every other credential stays as it was.)
    const auto* credential = principal.claims.if_contains("credential");
    if (access && credential && credential->is_string() &&
        access->PasswordMustChange(principal.name, std::string(credential->as_string()))) {
      answer["password_must_change"] = true;
    }
    finish();
    return Answer(out, kVerifiedExit, std::move(answer));
  } catch (const chargewire::Error& e) {
    const std::string unrecorded = record();
    finish();
    if (e.code() == chargewire::ErrorCode::kRateLimited) {
      return Answer(out, kRefusedExit,
                    {{"verified", false},
                     {"reason", "rate_limited"},
                     {"retry_after_seconds", e.retry_after() ? e.retry_after()->count() : 1},
                     {"message", e.what()}});
    }
    // The server's own refusal, with the reason it would give the client; a store that
    // stopped answering in between (permission_denied from a stale snapshot) is no answer.
    if (e.code() == chargewire::ErrorCode::kInvalidCredentials || e.code() == chargewire::ErrorCode::kInvalidToken) {
      // A failure the store doesn't hold would be a check that counts toward no limit.
      // (A store a server older than 2.14 made keeps no sign-in records at all: as before.)
      if (!unrecorded.empty() && !authz::IsMissingSignInRecords(unrecorded)) {
        return Undecided(out, StoreProblem(unrecorded),
                         "this check's failed sign-in could not be recorded in the store: " + unrecorded);
      }
      return Refused(out, e.reason().empty() ? std::string(chargewire::CodeName(e.code())) : e.reason(), e.what());
    }
    return Undecided(out, StoreProblem(e.what()) == "configuration" ? std::string(chargewire::CodeName(e.code()))
                                                                    : StoreProblem(e.what()),
                     e.what());
  } catch (const std::exception& e) {
    finish();
    return Undecided(out, StoreProblem(e.what()), e.what());
  }
}

}  // namespace hardcharger
