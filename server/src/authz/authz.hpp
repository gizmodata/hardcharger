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

#include <arrow/result.h>
#include <arrow/status.h>
#include <duckdb_v2.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include "authz/login_events.hpp"
#include "chargewire/backend.hpp"

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
  // The longest one store round trip may take (rbac_store_timeout_seconds).
  std::chrono::milliseconds store_timeout{10000};
  // This server's cluster_id: the store must belong to it unless store_shared.
  std::string cluster_id;
  bool store_shared = false;  // rbac_store_shared
  // rbac_max_snapshot_age_seconds: how long store users are served from the last snapshot
  // while the store does not answer (0: for as long as it takes).
  std::chrono::seconds max_snapshot_age{0};
  // No store: sign-in tokens' catalog_access rules restrict sessions (per-catalog
  // permissions), and every principal but the administrator is held to a
  // non-administrator's limits. Users, roles and grants don't exist; access-control
  // statements are refused.
  bool catalog_rules_only = false;
  // The store opened read-only, with no poller (hardcharger-server --verify-credentials): a
  // one-shot check of a credential that writes nothing, a user a sign-in token would
  // provision included (would_provision()).
  bool read_only = false;
  // With read_only: the login guard's sign-in records are still written, so a credential
  // check counts toward its limits (--verify-credentials).
  bool record_sign_ins = false;
  // The password policy (password.hpp): what CREATE USER and ALTER USER ... WITH PASSWORD
  // accept, and when a password stops signing in (0: never).
  int64_t password_min_length = 12;
  int64_t password_min_character_classes = 0;
  int64_t password_max_age_days = 0;
};

// What hardcharger_users() shows of a user's sign-ins (from the login guard).
struct SignInSummary {
  std::optional<int64_t> last_sign_in_ms;     // the last successful password sign-in
  int64_t failed_sign_ins = 0;                // failures the guard still counts
  std::optional<int64_t> last_failed_sign_in_ms;
};
using SignInSummaries = std::function<SignInSummary(const std::string& user_name)>;

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
  // The sign-in's role is readonly: the session changes nothing (and never acts as System
  // Admin).
  bool readonly = false;
  // The session's Chargewire id (s_...), for audit records; empty for the server's own
  // sessions (the init SQL's).
  std::string session_id;
};

// The store user attribute that keeps a provisioned user's token sign-ins readonly
// (token_auto_provision_users): the role of the token that created the user.
inline constexpr const char* kProvisionedRoleAttribute = "provisioned_role";

// Signs a scoped token for (user name, scope id, expiry in seconds since the epoch, the
// minter's certificate binding or empty).
using ScopedTokenSigner =
    std::function<std::string(const std::string&, const std::string&, int64_t, const std::string&)>;

// What the context functions a row or masking policy may call answer for one session,
// in plain SQL as in a policy: is_role_active(name), session_attribute(name). The
// administrator (and a session acting as System Admin, and every session on a server
// without access control) has every role active and no attributes.
struct SessionPolicyContext {
  bool all_roles_active = false;
  std::set<std::string> active_roles;  // folded names, as policies compare them
  std::map<std::string, std::string> attributes;
  // Is the role active in the session? Never says whether a role exists.
  bool RoleActive(const std::string& name) const;
  // The session's attribute, or nullopt when it has none of that name.
  std::optional<std::string> Attribute(const std::string& name) const;
  bool operator==(const SessionPolicyContext&) const = default;
};

// Replaces DuckDB's built-in current_user, session_user, user and current_role (each
// 'duckdb') with the session's own answers: the Hardcharger user (as hardcharger_user()),
// and the role SET ROLE made active, or NULL (as hardcharger_active_role()). Once, on the
// instance's system catalog, before any session runs.
void InstallPolicyContextFunctions(duckdb_v2_instance_handle instance);

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
  bool read_only() const { return read_only_; }
  // Read-only: whether a sign-in checked so far would have provisioned its user.
  bool would_provision() const { return would_provision_.load(); }

  // Password sign-in of a store user. nullopt for any failure (unknown user, wrong
  // password, disabled): the same answer, after the same work.
  struct SignIn {
    std::string user_name;
    std::string credential;  // for Identity::credential
    // The password was right, and older than password_max_age_days: refuse the sign-in.
    bool password_expired = false;
  };
  std::optional<SignIn> AuthenticatePassword(std::string_view user_name,
                                             std::string_view password) const;

  // A sign-in token's store user. `readonly`: the user's provisioned_role attribute
  // (kProvisionedRoleAttribute) is "readonly", so its token sign-ins are readonly whatever
  // role the token gives. `created`: ProvisionStoreUser created it just now.
  struct TokenUser {
    bool readonly = false;
    bool created = false;
  };

  // Refuses a sign-in token's user when it is not one of the store's enabled users.
  // Throws chargewire::Error(kInvalidCredentials).
  TokenUser RequireStoreUser(const std::string& user_name) const;

  // RequireStoreUser for token_auto_provision_users: a user the store doesn't have is
  // created first, with no password and no grants, and the token's role ("user" or
  // "readonly") in its provisioned_role attribute. A disabled user stays disabled (and is
  // refused). Throws chargewire::Error(kInvalidCredentials) when the user can't be created
  // (its name is a role's, the administrator's, or an unknown tenant's) or is disabled.
  // Each creation, and each refusal to create, is an audit record naming `issuer`.
  TokenUser ProvisionStoreUser(const std::string& user_name, const std::string& token_role,
                               const std::string& issuer);

  // A client certificate's identity (spec §4.4): the store's name of the enabled user it
  // names, or nullopt. An e-mail or CN identity compares as user names do (ignoring ASCII
  // case); a URI identity must match exactly (Error(kUnknownCertificateIdentity) when it
  // differs only in case). A user signs in this way with or without a password.
  std::optional<std::string> CertificateUser(const std::string& identity) const;

  // Throws chargewire::Error(kInvalidToken), with Chargewire §4.2's reason, when an access
  // token for `who` no longer stands: the user was dropped or disabled
  // (principal_dropped), the password it was issued against was changed
  // (credentials_changed), or its scoped token was revoked (token_revoked) or expired
  // (token_expired). Only for a token this server signed. Cheap: called on every request.
  // Returns the store user the token names right now (empty for the administrator and on
  // catalog_rules_only servers), for the token's authority (AuthorityKey in auth.hpp): a
  // user dropped and created again under the same name is another user.
  struct TokenStanding {
    std::string user_id;
    std::string tenant_id;
  };
  TokenStanding CheckToken(const Identity& who) const;

  // A sign-in with this credential (Identity::credential) is to a temporary password: its
  // sessions run nothing but the statement that changes it (SessionGuard::PasswordMustChange).
  bool PasswordMustChange(const std::string& user_name, const std::string& credential) const;

  // Binds a new session's connection to `who`. Throws chargewire::Error when the session
  // is refused (unknown or disabled user, revoked scoped token, policy service).
  std::shared_ptr<SessionGuard> Bind(duckdb_v2_connection_handle connection, Identity who);

  const std::string& store_description() const;
  // A DuckDB store's file (absolute), which SQL must never reach; empty for PostgreSQL.
  const std::string& store_file() const;
  // The cluster the store belongs to (the first cluster_id that opened it), or empty.
  const std::string& store_cluster_id() const;

  // The store's health, for metrics: whether its last poll answered, and how long ago it
  // last did (the age of the grants this server holds).
  struct StoreHealth {
    bool up = true;
    double snapshot_age_seconds = 0;
  };
  StoreHealth store_health() const;

  // The login guard's sign-in records in the store (login_events.hpp); see RbacStore.
  arrow::Status AppendLoginEvents(const std::vector<LoginEventRecord>& events);
  arrow::Result<std::vector<LoginEventRecord>> LoginEventsSince(int64_t since_ms);
  arrow::Status PruneLoginEvents(int64_t failures_before_ms, int64_t successes_before_ms);

  // Where hardcharger_users() reads each user's sign-ins from (unset: no values).
  void SetSignInSummaries(SignInSummaries summaries);

 private:
  void OnSnapshot(const Snapshot& snapshot);
  void EndSessionsOf(const std::string& user_id, const std::string& reason);
  friend class SessionGuard;

  std::shared_ptr<RbacEngine> engine_;
  bool catalog_rules_only_ = false;
  bool read_only_ = false;
  std::atomic<bool> would_provision_{false};
  std::vector<std::string> hidden_catalogs_;
  ScopedTokenSigner signer_;
  SignInSummaries sign_in_summaries_;
  int64_t password_max_age_days_ = 0;
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

  // Throws chargewire::Error(kSessionEnded) once an access-control change ended the session.
  void RequireLive() const;

  // For a session that said SET ROLE SYSTEM ADMIN: reads the store's version and loads
  // what changed, so that what the session does next rests on the store's own word and not
  // on what this server last polled (a revoke on another replica counts at once), and
  // notes when the store did not answer: the session then acts as nobody until it does.
  // Does nothing for any other session. Called at the start of every request, on the
  // session's engine thread, before RequireLive.
  // The same for a session whose principal holds a policy exemption (EXEMPT ROW POLICIES,
  // EXEMPT MASKING POLICIES): while the store does not answer, no exemption applies.
  void VerifySystemAdmin() const;
  // What is_role_active(), session_attribute() and the policies answer for this session now.
  SessionPolicyContext PolicyContextNow() const;
  // Throws permission_denied, saying why, for a session that said SET ROLE SYSTEM ADMIN and
  // does not act as one (its user no longer holds it, or the store did not answer): such a
  // session runs nothing until RESET ROLE. For the commands that are not SQL statements of
  // DuckDB's or access control's (SET hardcharger.*, KILL SESSION), which pass no other check.
  void RefuseIfSystemAdminLost() const;

  // The Chargewire id of the statement the session runs now (its sql_statements row), which
  // the access-control audit records carry as statement_id; empty: none. The session sets it
  // around each access-control statement.
  void SetRequestId(std::string id) { request_id_ = std::move(id); }

  // The session signed in with a temporary password (one someone else set: a delegate, or an
  // administrator WITH ... EXPIRE) that is still the user's: it must change it before doing
  // anything else. Asked of the current snapshot each time.
  bool PasswordMustChange() const;
  // When the password must change, throws (permission denied, saying how to change it) for
  // anything but the session's own change (`sql`: ALTER USER CURRENT_USER | <own name> WITH
  // PASSWORD '...' REPLACE '...'); `sql` nullopt is a request that runs no statement text
  // (prepare, a prepared statement, metadata, ingest), refused too. Each refusal is audited.
  void RefuseUntilPasswordChanged(std::string_view what, std::optional<std::string_view> sql) const;

  // Runs one statement text. An access-control statement (GRANT, CREATE USER, SHOW
  // GRANTS, ...) runs here; anything else passes the pre-bind gate and is run by `run`
  // (DuckDB), under the plan check.
  chargewire::StatementOutcome Execute(std::string_view sql, bool has_parameters,
                                    const std::function<chargewire::StatementOutcome()>& run,
                                    const boost::json::value& parameters = {});

  // Prepares one statement: an access-control statement is kept to run at execution;
  // anything else passes the pre-bind gate and is prepared by `prepare`.
  std::unique_ptr<chargewire::PreparedStatement> Prepare(
      std::string_view sql,
      const std::function<std::unique_ptr<chargewire::PreparedStatement>()>& prepare);

  // Executes what Prepare returned; `run` executes the DuckDB statement it wraps.
  chargewire::StatementOutcome ExecutePrepared(
      chargewire::PreparedStatement& statement, bool has_parameters,
      const std::function<chargewire::StatementOutcome(chargewire::PreparedStatement&)>& run,
      const boost::json::value& parameters = {});

  // A bulk ingest (one SQL statement inside `run`, under the plan check).
  int64_t Ingest(const chargewire::IngestTarget& target, const std::function<int64_t()>& run);

  // Metadata: `run` looks it up as Hardcharger's own (unchecked) query; only what the
  // session's principal may access is kept (Chargewire §9.4), by the rules SQL applies. For
  // tables with schemas, `run` lists names only and `describe` adds the schemas of the
  // tables that are kept, which keep only the columns the principal may see.
  std::shared_ptr<arrow::RecordBatchReader> Metadata(
      chargewire::MetadataKind kind, const chargewire::MetadataFilter& filter,
      const std::function<
          std::shared_ptr<arrow::RecordBatchReader>(const chargewire::MetadataFilter&)>& run,
      const std::function<std::shared_ptr<arrow::RecordBatchReader>(
          std::shared_ptr<arrow::RecordBatchReader>)>& describe);

  // An error as this session's user may see it: for a tenant's user, one that names
  // another tenant's objects reads exactly like one about something that does not exist.
  chargewire::Error ForTenant(const chargewire::Error& error) const;

  // Runs Hardcharger's own statements on the session's connection (its profiler
  // settings, ...) unchecked: they are the server's, not the user's.
  void RunInternal(const std::function<void()>& run);
  // One-way connection restriction, independent of the principal's privileges. A session
  // acting as System Admin stops doing so: a read-only session never does.
  void SetReadOnly();
  // An administrative command the session ran outside SQL's statements (KILL SESSION, SET
  // GLOBAL hardcharger.*): an audit record when it ran it acting as System Admin, naming
  // its user. `fields`: what it acted on (never a secret).
  void AuditAdministrativeCommand(std::string_view command,
                                  std::vector<std::pair<std::string, std::string>> fields = {}) const;

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
  // rules, or a store user acting as System Admin right now. A restricted administrator
  // (admin token with rules) is false. Safe from any thread.
  bool unrestricted() const { return unrestricted_ || system_admin(); }
  // True while a store user's session acts as System Admin: it said SET ROLE SYSTEM ADMIN
  // and its user holds SYSTEM ADMIN in the grants the server holds at this call (asked of
  // them each time; nothing is remembered). Never true for the administrator's own
  // sessions. Safe from any thread.
  bool system_admin() const;
  // What SET ROLE made active: a role's name, "SYSTEM ADMIN" (whether or not its user
  // still holds it), or empty for all of the user's roles. Safe from any thread.
  std::string active_role() const;
  // The tenant the session's user belongs to, by name ("" for none).
  const std::string& tenant_name() const { return tenant_name_; }
  // The session started in its tenant's default catalog (ALTER TENANT ... SET DEFAULT CATALOG).
  bool started_in_tenant_catalog() const { return started_in_tenant_catalog_; }

 private:
  SessionGuard(AccessControl& owner, duckdb_v2_connection_handle connection, Identity identity,
               std::string user_id);
  friend class AccessControl;

  std::shared_ptr<void> MarkBusy();
  bool busy() const;
  void End(const std::string& reason);
  void InterruptRunning();
  void CheckBeforeBind(std::string_view sql, const boost::json::value& parameters = {}, bool preparing = false);
  chargewire::StatementOutcome RunCommand(const ParsedCommand& command);
  void CheckReadOnlyCommand(const ParsedCommand& command) const;
  std::shared_ptr<arrow::RecordBatchReader> Track(std::shared_ptr<arrow::RecordBatchReader> rows,
                                                  std::shared_ptr<void> busy);
  void AuditAllowed();
  void AuditDenied(const chargewire::Error& error, std::string_view sql);
  // Who every audit record of this session names: the user (its name and store id), the
  // session, and that it acts as System Admin when it does.
  std::vector<std::pair<std::string, std::string>> AuditWho(bool as_system_admin) const;
  // The administrative commands (ATTACH, SET GLOBAL, ...) among `sql`'s statements, for
  // the record of a statement run as System Admin.
  std::string AdministrativeCommands(std::string_view sql) const;
  void AuditSystemAdminStatement(std::string_view sql, const std::string& commands,
                                 const chargewire::Error* error) const;
  // Writes the record that ends the session's time as System Admin, once.
  void EndSystemAdmin(const std::string& reason);
  // SET ROLE SYSTEM ADMIN: why this session may not (its own limits), or nullopt.
  std::optional<std::string> SystemAdminRefusal() const;

  AccessControl& owner_;
  const Identity identity_;
  bool unrestricted_ = false;  // the superuser: an administrator without catalog_access rules
  bool read_only_ = false;  // session engine thread only
  // A "System Admin elevation" audit record was written and its end was not yet.
  std::atomic<bool> system_admin_open_{false};
  const std::string user_id_;  // empty for the administrator
  std::string credential_;     // the user's password hash when the session started
  std::string tenant_id_;
  std::string tenant_name_;
  bool started_in_tenant_catalog_ = false;
  int64_t scope_expires_at_ = 0;  // a scoped token's session: when the scope ends (Unix s)
  std::string tenant_catalogs_;
  // The statement being run (SetRequestId), for the access-control audit records; engine
  // thread only.
  std::string request_id_;

  mutable std::mutex mutex_;  // guards connection_ and ended_ against the store's listener
  duckdb_v2_connection_handle connection_;
  std::optional<std::string> ended_;  // why the session ended
  std::shared_ptr<std::atomic<int>> busy_;
};

}  // namespace hardcharger::authz
