// Role-based access control: users, roles and grants in SQL (CREATE USER,
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
  // mtls_admin_identities: client certificate identities that are the administrator too.
  std::vector<std::string> certificate_administrators;
  // Open Policy Agent decision URL asked after Hardcharger's own check; empty: none.
  std::string policy_service_url;
  std::chrono::milliseconds policy_service_timeout{2000};
  // How often to look for changes other replicas made.
  std::chrono::milliseconds poll_interval{1000};
  // No store: sign-in tokens' catalog_access rules restrict sessions (per-catalog
  // permissions), and every principal but the administrator is held to a
  // non-administrator's limits. Users, roles and grants don't exist; access-control
  // statements are refused.
  bool catalog_rules_only = false;
};

// One catalog_access rule of a sign-in token: a catalog name or glob ('*', '?') and
// "read", "write" or "none". The first rule that matches a catalog decides.
struct CatalogAccessRule {
  std::string catalog;
  std::string access;
};

// Who a session acts for, from its access token.
struct Identity {
  std::string user_name;
  bool superuser = false;  // the administrator (token role "admin")
  // The fingerprint of the user's password the token was issued against; empty for
  // tokens that did not come from a password sign-in.
  std::string credential;
  std::string scope_id;  // a scoped token's store record
  // The client certificate the access token is bound to (x5t#S256), or empty. Scoped
  // tokens this identity mints carry it too.
  std::string certificate_thumbprint;
  std::string idp_issuer;
  std::vector<std::string> idp_groups;
  // The token's catalog_access rules (catalog_rules_only servers; ignored under RBAC).
  std::optional<std::vector<CatalogAccessRule>> catalog_access;
};

// Signs a scoped token for (user name, scope id, expiry in seconds since the epoch, the
// minter's certificate binding or empty).
using ScopedTokenSigner =
    std::function<std::string(const std::string&, const std::string&, int64_t, const std::string&)>;

// On a server without access control: the kind of access-control statement `sql` is
// ("CREATE ROLE", ...), so it can be refused with a clear error, or nullopt for SQL
// DuckDB should have (SHOW GRANTS and ALTER ... OWNER TO are DuckDB's there too).
std::optional<std::string> AccessControlStatementKind(std::string_view sql);

// On a server with access control: whether `sql` is one of the statements the session
// guard runs itself (GRANT, CREATE USER, SHOW GRANTS, ...), which DuckDB can't parse.
bool LooksLikeAccessControlStatement(std::string_view sql);

}  // namespace hardcharger::authz

namespace hardcharger {
class PrivateFiles;
class SensitivePaths;
}

namespace hardcharger::authz {

// The access-control introspection table functions (hardcharger_grants(), ...), each
// read from the store snapshot the server holds, keeping only what the session may see
// (introspection.hpp has the rules).
enum class AccessTable { kGrants, kRoleMembers, kRoles, kUsers, kPolicies, kEffectivePrivileges };

// The SQL name of each ("hardcharger_grants", ...).
const char* AccessTableName(AccessTable table);

// The table with no rows: what a server without an access-control store returns.
std::shared_ptr<arrow::RecordBatch> EmptyAccessTable(AccessTable table);

// The file a DuckDB store URI names ("duckdb:<path>", or a path ending in .duckdb), as
// given; empty for any other store.
std::string DuckDBStoreFile(const std::string& store_uri);

// Puts the guard of the server's own files (PrivateFiles, guards.hpp) and of the
// sensitive paths (SensitivePaths, once `sensitive` is enforcing) into `instance`'s file
// system: no statement opens, lists, globs, stats, moves or removes them, whatever path
// it computes (file_guard.cc). Paths added to either later are guarded from then on.
void InstallFileGuard(duckdb_v2_instance_handle instance, std::shared_ptr<const PrivateFiles> files,
                      std::shared_ptr<const SensitivePaths> sensitive);

// A "readonly" principal's session (read_only.cc): from now on DuckDB's own analysis of
// every statement it prepares on `connection` decides whether it writes, as in GizmoSQL
// 1.x. One that modifies a database (the temp catalog included) or writes a file, wherever
// in the statement (a data-modifying CTE, COPY ... TO inside a CTE), is refused before it
// runs: "User '<user_name>' has a readonly session and cannot run statements that modify
// state." Called once, before any statement runs on the connection.
void BindReadOnlySession(duckdb_v2_connection_handle connection, const std::string& user_name);

// Whether a readonly session may run `sql`, one SET or RESET of a DuckDB setting: only a
// setting DuckDB keeps for the session (SET SESSION, or a plain SET of a setting whose
// default scope is the session) that doesn't reach the host or the instance, switch the
// grammar or write files. Never GLOBAL.
bool ReadOnlySessionMaySet(duckdb_v2_connection_handle connection, std::string_view sql);

class AccessControl {
 public:
  // Opens the store and loads it. Throws std::runtime_error (never quoting the store's
  // credentials) when it cannot.
  explicit AccessControl(const Options& options);
  ~AccessControl();

  // Stops access control's background work (the store poller) and waits for it. Call on
  // every way out of the server, before the DuckDB instances are torn down.
  void Shutdown();
  AccessControl(const AccessControl&) = delete;
  AccessControl& operator=(const AccessControl&) = delete;

  // Registers the plan check on `instance`. Call once, right after the instance is
  // opened and before any session connection exists.
  void InstallEnforcement(duckdb_v2_instance_handle instance);

  // How CREATE SCOPED TOKEN signs the tokens it hands out.
  void SetScopedTokenSigner(ScopedTokenSigner signer);

  // The catalogs the server keeps for itself (instrumentation, logs): sessions bound
  // after this see them only as the administrator.
  void SetHiddenCatalogs(std::vector<std::string> catalogs);

  // Without a store: catalog_access rules only (Options::catalog_rules_only).
  bool catalog_rules_only() const { return catalog_rules_only_; }

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

  // A client certificate's identity (spec §4.4): the store's name of the enabled user it
  // names, or nullopt. An e-mail or CN identity compares as user names do (ignoring ASCII
  // case); a URI identity must match exactly (Error(kUnknownCertificateIdentity) when it
  // differs only in case). A user signs in this way with or without a password.
  std::optional<std::string> CertificateUser(const std::string& identity) const;

  // Throws warpsql::Error(kInvalidToken), with Warp SQL §4.2's reason, when an access
  // token for `who` no longer stands: the user was dropped or disabled
  // (principal_dropped), the password it was issued against was changed
  // (credentials_changed), or its scoped token was revoked (token_revoked) or expired
  // (token_expired). Only for a token this server signed. Cheap: called on every request.
  void CheckToken(const Identity& who) const;

  // Binds a new session's connection to `who`. Throws warpsql::Error when the session
  // is refused (unknown or disabled user, revoked scoped token, policy service).
  std::shared_ptr<SessionGuard> Bind(duckdb_v2_connection_handle connection, Identity who);

  const std::string& store_description() const;
  // A DuckDB store's file (absolute), which SQL must never reach; empty for PostgreSQL.
  const std::string& store_file() const;

 private:
  void OnSnapshot(const Snapshot& snapshot);
  void EndSessionsOf(const std::string& user_id, const std::string& reason);
  friend class SessionGuard;

  std::shared_ptr<RbacEngine> engine_;
  bool catalog_rules_only_ = false;
  std::vector<std::string> hidden_catalogs_;
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

  // Metadata: `run` looks it up as Hardcharger's own (unchecked) query; only what the
  // session's principal may access is kept (Warp SQL §9.4), by the rules SQL applies. For
  // tables with schemas, `run` lists names only and `describe` adds the schemas of the
  // tables that are kept, which keep only the columns the principal may see.
  std::shared_ptr<arrow::RecordBatchReader> Metadata(
      warpsql::MetadataKind kind, const warpsql::MetadataFilter& filter,
      const std::function<
          std::shared_ptr<arrow::RecordBatchReader>(const warpsql::MetadataFilter&)>& run,
      const std::function<std::shared_ptr<arrow::RecordBatchReader>(
          std::shared_ptr<arrow::RecordBatchReader>)>& describe);

  // An error as this session's user may see it: for a tenant's user, one that names
  // another tenant's objects reads exactly like one about something that does not exist.
  warpsql::Error ForTenant(const warpsql::Error& error) const;

  // Runs Hardcharger's own statements on the session's connection (its profiler
  // settings, ...) unchecked: they are the server's, not the user's.
  void RunInternal(const std::function<void()>& run);

  // The rows of an introspection table this session may see, for the principal it acts as
  // now (its SET ROLE role included). `user_name`: hardcharger_effective_privileges()'s
  // argument. Safe from DuckDB's threads while one of the session's statements runs.
  std::shared_ptr<arrow::RecordBatch> AccessTableRows(AccessTable table,
                                                      const std::optional<std::string>& user_name) const;

  // The session's connection is going away; nothing may touch it after this.
  void Detach();

  const std::string& user_name() const { return identity_.user_name; }
  bool superuser() const { return identity_.superuser; }  // the token's role is admin (rules or not)
  // True when this session is allowed everything: the administrator without catalog_access
  // rules. A restricted administrator (admin token with rules) is false.
  bool unrestricted() const { return unrestricted_; }
  // The tenant the session's user belongs to, by name ("" for none).
  const std::string& tenant_name() const { return tenant_name_; }

 private:
  SessionGuard(AccessControl& owner, duckdb_v2_connection_handle connection, Identity identity,
               std::string user_id);
  friend class AccessControl;

  std::shared_ptr<void> MarkBusy();
  bool busy() const;
  void End(const std::string& reason);
  void InterruptRunning();
  void CheckBeforeBind(std::string_view sql);
  warpsql::StatementOutcome RunCommand(const ParsedCommand& command);
  std::shared_ptr<arrow::RecordBatchReader> Track(std::shared_ptr<arrow::RecordBatchReader> rows,
                                                  std::shared_ptr<void> busy);
  void AuditAllowed();
  void AuditDenied(const warpsql::Error& error, std::string_view sql);

  AccessControl& owner_;
  const Identity identity_;
  bool unrestricted_ = false;  // the superuser: an administrator without catalog_access rules
  const std::string user_id_;  // empty for the administrator
  std::string credential_;     // the user's password hash when the session started
  std::string tenant_id_;
  std::string tenant_name_;
  int64_t scope_expires_at_ = 0;  // a scoped token's session: when the scope ends (Unix s)
  std::string tenant_catalogs_;

  mutable std::mutex mutex_;  // guards connection_ and ended_ against the store's listener
  duckdb_v2_connection_handle connection_;
  std::optional<std::string> ended_;  // why the session ended
  std::shared_ptr<std::atomic<int>> busy_;
};

}  // namespace hardcharger::authz
