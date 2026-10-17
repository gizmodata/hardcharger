// Role-based access control (Enterprise): users, roles and grants in SQL (CREATE USER,
// GRANT, REVOKE, SHOW GRANTS, ...), kept in a PostgreSQL store shared by every replica,
// and checked inside DuckDB against each statement's own plan.
//
// This is the only access-control header the rest of Hardcharger includes. It names
// DuckDB only through v2 C API handles; the module behind it (src/authz/) is the one
// part of Hardcharger that uses DuckDB's internal C++ headers, because the check reads
// DuckDB's logical plans.
//
// How a session is protected:
//  - AccessControl::Bind gives a new session's connection its principal, as DuckDB
//    state that SQL cannot reach, before any statement runs on it. A session that
//    cannot be bound is refused.
//  - Every statement is checked when DuckDB binds it and again before it optimizes it;
//    whatever the check cannot classify is refused, and a plan that never reached the
//    check is refused when it is prepared.
//  - Statement families that reach outside the database (files, URLs, ATTACH, ...) are
//    refused before DuckDB binds them, since binding them already has effects.
//  - When the store changes (here or on another replica), sessions of users who were
//    dropped, disabled or changed their password end, statements of users who lost a
//    right stop, and their tokens stop working.
#pragma once

#include <duckdb_v2.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "warpsql/backend.hpp"

namespace hardcharger::authz {

class RbacEngine;
class Snapshot;
class SessionGuard;
struct ParsedCommand;

struct Options {
  // PostgreSQL connection string or URI of the store ("memory": in-process, tests only).
  std::string store_uri;
  // The server administrator's user name, which no store user may take.
  std::string administrator_name;
  // Open Policy Agent decision URL asked after Hardcharger's own check; empty: none.
  std::string policy_service_url;
  std::chrono::milliseconds policy_service_timeout{2000};
  // How often to look for changes other replicas made.
  std::chrono::milliseconds poll_interval{1000};
};

// Who a session acts for, from its access token.
struct Identity {
  std::string user_name;
  bool superuser = false;  // the administrator (token role "admin")
  // The fingerprint of the user's password the token was issued against; empty for
  // tokens that did not come from a password sign-in.
  std::string credential;
  std::string scope_id;  // a scoped token's store record
  std::string idp_issuer;
  std::vector<std::string> idp_groups;
};

// Signs a scoped token for (user name, scope id, expiry in seconds since the epoch).
using ScopedTokenSigner =
    std::function<std::string(const std::string&, const std::string&, int64_t)>;

// On a server without access control: the kind of access-control statement `sql` is
// ("CREATE ROLE", ...), so it can be refused with a clear error, or nullopt for SQL
// DuckDB should have (SHOW GRANTS and ALTER ... OWNER TO are DuckDB's there too).
std::optional<std::string> AccessControlStatementKind(std::string_view sql);

class AccessControl {
 public:
  // Opens the store and loads it. Throws std::runtime_error (never quoting the store's
  // credentials) when it cannot.
  explicit AccessControl(const Options& options);
  ~AccessControl();
  AccessControl(const AccessControl&) = delete;
  AccessControl& operator=(const AccessControl&) = delete;

  // Registers the plan check on `instance`. Call once, right after the instance is
  // opened and before any session connection exists.
  void InstallEnforcement(duckdb_v2_instance_handle instance);

  // How CREATE SCOPED TOKEN signs the tokens it hands out.
  void SetScopedTokenSigner(ScopedTokenSigner signer);

  // Password sign-in of a store user. nullopt for any failure (unknown user, wrong
  // password, disabled): the same answer, after the same work.
  struct SignIn {
    std::string user_name;
    std::string credential;  // for Identity::credential
  };
  std::optional<SignIn> AuthenticatePassword(std::string_view user_name,
                                             std::string_view password) const;

  // Refuses a sign-in token's user when it is not one of the store's enabled users.
  // Throws warpsql::Error(kInvalidCredentials).
  void RequireStoreUser(const std::string& user_name) const;

  // Throws warpsql::Error(kInvalidToken) when an access token for `who` no longer
  // stands: the user was dropped or disabled, the password it was issued against was
  // changed, or its scoped token was revoked or expired. Cheap: called on every request.
  void CheckToken(const Identity& who) const;

  // Binds a new session's connection to `who`. Throws warpsql::Error when the session
  // is refused (unknown or disabled user, revoked scoped token, policy service).
  std::shared_ptr<SessionGuard> Bind(duckdb_v2_connection_handle connection, Identity who);

  const std::string& store_description() const;

 private:
  void OnSnapshot(const Snapshot& snapshot);
  void EndSessionsOf(const std::string& user_id, const std::string& reason);
  friend class SessionGuard;

  std::shared_ptr<RbacEngine> engine_;
  ScopedTokenSigner signer_;
  std::mutex sessions_mutex_;
  std::vector<std::weak_ptr<SessionGuard>> sessions_;
  std::shared_ptr<const Snapshot> seen_;  // the snapshot sessions were last compared with
};

// One session's side of access control: everything its statements go through. Every
// method may be called only on the session's engine thread, except Interrupt-like work
// the store's listener does through AccessControl.
class SessionGuard : public std::enable_shared_from_this<SessionGuard> {
 public:
  ~SessionGuard();

  // Throws warpsql::Error(kSessionEnded) once an access-control change ended the session.
  void RequireLive() const;

  // Runs one statement text. An access-control statement (GRANT, CREATE USER, SHOW
  // GRANTS, ...) runs here; anything else passes the pre-bind gate and is run by `run`
  // (DuckDB), under the plan check.
  warpsql::StatementOutcome Execute(std::string_view sql, bool has_parameters,
                                    const std::function<warpsql::StatementOutcome()>& run);

  // Prepares one statement: an access-control statement is kept to run at execution;
  // anything else passes the pre-bind gate and is prepared by `prepare`.
  std::unique_ptr<warpsql::PreparedStatement> Prepare(
      std::string_view sql,
      const std::function<std::unique_ptr<warpsql::PreparedStatement>()>& prepare);

  // Executes what Prepare returned; `run` executes the DuckDB statement it wraps.
  warpsql::StatementOutcome ExecutePrepared(
      warpsql::PreparedStatement& statement, bool has_parameters,
      const std::function<warpsql::StatementOutcome(warpsql::PreparedStatement&)>& run);

  // A bulk ingest (one SQL statement inside `run`, under the plan check).
  int64_t Ingest(const warpsql::IngestTarget& target, const std::function<int64_t()>& run);

  // Metadata: `run` looks it up as Hardcharger's own (unchecked) query; what a tenant's
  // user may not see is left out.
  std::shared_ptr<arrow::RecordBatchReader> Metadata(
      warpsql::MetadataKind kind, const warpsql::MetadataFilter& filter,
      const std::function<std::shared_ptr<arrow::RecordBatchReader>(const warpsql::MetadataFilter&)>&
          run);

  // The session's connection is going away; nothing may touch it after this.
  void Detach();

  const std::string& user_name() const { return identity_.user_name; }
  bool superuser() const { return identity_.superuser; }

 private:
  SessionGuard(AccessControl& owner, duckdb_v2_connection_handle connection, Identity identity,
               std::string user_id);
  friend class AccessControl;

  std::shared_ptr<void> MarkBusy();
  bool busy() const;
  void End(const std::string& reason);
  void InterruptRunning();
  void CheckBeforeBind(std::string_view sql);
  warpsql::Error ForTenant(const warpsql::Error& error) const;
  warpsql::StatementOutcome RunCommand(const ParsedCommand& command);
  std::shared_ptr<arrow::RecordBatchReader> Track(std::shared_ptr<arrow::RecordBatchReader> rows,
                                                  std::shared_ptr<void> busy);
  void AuditAllowed();
  void AuditDenied(const warpsql::Error& error, std::string_view sql);

  AccessControl& owner_;
  const Identity identity_;
  const std::string user_id_;  // empty for the administrator
  std::string credential_;     // the user's password hash when the session started
  std::string tenant_id_;
  std::string tenant_catalogs_;

  mutable std::mutex mutex_;  // guards connection_ and ended_ against the store's listener
  duckdb_v2_connection_handle connection_;
  std::optional<std::string> ended_;  // why the session ended
  std::shared_ptr<std::atomic<int>> busy_;
};

}  // namespace hardcharger::authz
