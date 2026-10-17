#include "authz/authz.hpp"

#include <arrow/api.h>
#include <openssl/evp.h>

#include <duckdb.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database_manager.hpp>

#include "audit.hpp"
#include "authz/ddl.hpp"
#include "authz/enforcement.hpp"
#include "authz/engine.hpp"
#include "authz/gate.hpp"
#include "authz/resolver.hpp"
#include "duckdb_sql.hpp"
#include "logging.hpp"

namespace hardcharger::authz {

namespace dd = duckdb;
using warpsql::Error;
using warpsql::ErrorCode;

// An access-control statement, parsed: run at Execute, or kept by Prepare.
struct ParsedCommand {
  RbacCommand command;
  std::string kind;  // "GRANT", "CREATE USER", ... for audit records (never the text)
};

namespace {

int64_t NowSeconds() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// What a token carries of a user's password: a fingerprint of its stored hash, so a
// token stops working when the password changes, and the hash itself never leaves.
std::string Fingerprint(const std::optional<std::string>& password_hash) {
  if (!password_hash || password_hash->empty()) return {};
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned int length = 0;
  EVP_Digest(password_hash->data(), password_hash->size(), digest, &length, EVP_sha256(), nullptr);
  static constexpr char kHex[] = "0123456789abcdef";
  std::string hex;
  for (unsigned int i = 0; i < 16 && i < length; i++) {
    hex += kHex[digest[i] >> 4];
    hex += kHex[digest[i] & 15];
  }
  return hex;
}

// An access-control failure as a Warp SQL error: refusals are permission_denied, the
// rest (unknown names, malformed statements) are SQL errors.
Error FromStatus(const arrow::Status& status, const char* sqlstate = "") {
  const std::string& message = status.message();
  if (message.find("Hardcharger blocked") != std::string::npos) {
    return Error(ErrorCode::kPermissionDenied, message, "42501");
  }
  return Error(ErrorCode::kSqlError, message, sqlstate);
}

dd::ClientContext& ContextOf(duckdb_v2_connection_handle connection) {
  return *dd::capiv2::Convert(connection)->context;
}

dd::shared_ptr<RbacSessionState> StateOf(duckdb_v2_connection_handle connection) {
  return ContextOf(connection).registered_state->Get<RbacSessionState>(kRbacSessionStateName);
}

std::string Quoted(const std::string& name) {
  std::string out = "\"";
  for (char c : name) out += c == '"' ? std::string("\"\"") : std::string(1, c);
  return out + "\"";
}

// The catalogs a tenant may see, as one comparable string.
std::string TenantCatalogsKey(const Snapshot& snapshot, const std::string& tenant_id) {
  std::string key;
  for (const auto& catalog : snapshot.TenantCatalogs(tenant_id)) key += catalog + "\x1f";
  return key;
}

// A command's rows as a reader of text columns.
std::shared_ptr<arrow::RecordBatchReader> Rows(const CommandResult& result) {
  arrow::FieldVector fields;
  for (const auto& name : result.column_names) fields.push_back(arrow::field(name, arrow::utf8()));
  auto schema = arrow::schema(fields);
  arrow::ArrayVector columns;
  for (std::size_t c = 0; c < result.column_names.size(); c++) {
    arrow::StringBuilder builder;
    for (const auto& row : result.rows) {
      if (!builder.Append(c < row.size() ? row[c] : std::string()).ok()) {
        throw Error(ErrorCode::kExecutionError, "could not build the result");
      }
    }
    std::shared_ptr<arrow::Array> array;
    if (!builder.Finish(&array).ok())
      throw Error(ErrorCode::kExecutionError, "could not build the result");
    columns.push_back(std::move(array));
  }
  auto batch = arrow::RecordBatch::Make(schema, static_cast<int64_t>(result.rows.size()), columns);
  auto reader = arrow::RecordBatchReader::Make({batch}, schema);
  if (!reader.ok()) throw Error(ErrorCode::kExecutionError, reader.status().message());
  return *reader;
}

// A client-prepared statement under access control: the DuckDB statement and what its
// first successful run must record (who owns what it creates).
class GuardedPrepared : public warpsql::PreparedStatement {
 public:
  GuardedPrepared(std::unique_ptr<warpsql::PreparedStatement> inner,
                  std::vector<PendingOwnership> ownership)
      : inner_(std::move(inner)), ownership_(std::move(ownership)) {}
  std::shared_ptr<arrow::Schema> ParameterSchema() override { return inner_->ParameterSchema(); }
  std::shared_ptr<arrow::Schema> ResultSchema() override { return inner_->ResultSchema(); }

  warpsql::PreparedStatement& inner() { return *inner_; }
  std::vector<PendingOwnership> TakeOwnership() { return std::move(ownership_); }

 private:
  std::unique_ptr<warpsql::PreparedStatement> inner_;
  std::vector<PendingOwnership> ownership_;
};

// A prepared access-control statement: it runs when executed, as 1.x runs it when
// prepared would run it twice for clients that prepare and then execute.
class PreparedAccessControl : public warpsql::PreparedStatement {
 public:
  explicit PreparedAccessControl(ParsedCommand command) : command_(std::move(command)) {}
  std::shared_ptr<arrow::Schema> ParameterSchema() override { return arrow::schema({}); }
  std::shared_ptr<arrow::Schema> ResultSchema() override { return nullptr; }
  const ParsedCommand& command() const { return command_; }

 private:
  ParsedCommand command_;
};

// Rows of a statement still being read: the session counts as running SQL until they
// are read or dropped, so a revoke stops it.
class TrackedReader : public arrow::RecordBatchReader {
 public:
  TrackedReader(std::shared_ptr<arrow::RecordBatchReader> rows, std::shared_ptr<void> busy)
      : rows_(std::move(rows)), busy_(std::move(busy)) {}
  std::shared_ptr<arrow::Schema> schema() const override { return rows_->schema(); }
  arrow::Status ReadNext(std::shared_ptr<arrow::RecordBatch>* batch) override {
    auto status = rows_->ReadNext(batch);
    if (!status.ok() || *batch == nullptr) busy_.reset();
    return status;
  }
  arrow::Status Close() override {
    busy_.reset();
    return rows_->Close();
  }

 private:
  std::shared_ptr<arrow::RecordBatchReader> rows_;
  std::shared_ptr<void> busy_;
};

// The rows of `rows` for which `keep(batch, row)` holds, in order.
std::shared_ptr<arrow::RecordBatchReader> FilterRows(
    std::shared_ptr<arrow::RecordBatchReader> rows,
    const std::function<bool(const arrow::RecordBatch&, int64_t)>& keep) {
  auto schema = rows->schema();
  arrow::RecordBatchVector kept;
  for (;;) {
    auto next = rows->Next();
    if (!next.ok()) throw Error(ErrorCode::kExecutionError, next.status().message());
    auto batch = std::move(next).ValueUnsafe();
    if (batch == nullptr) break;
    for (int64_t row = 0; row < batch->num_rows(); row++) {
      if (keep(*batch, row)) kept.push_back(batch->Slice(row, 1));
    }
  }
  auto reader = arrow::RecordBatchReader::Make(std::move(kept), schema);
  if (!reader.ok()) throw Error(ErrorCode::kExecutionError, reader.status().message());
  return *reader;
}

// Does `text` contain `word` as a whole word (identifier characters around it don't
// count), ignoring case?
bool ContainsWord(const std::string& text, const std::string& word) {
  if (word.empty()) return false;
  auto lower = [](std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
  };
  const std::string haystack = lower(text), needle = lower(word);
  auto identifier = [](char c) { return std::isalnum(static_cast<unsigned char>(c)) || c == '_'; };
  for (auto at = haystack.find(needle); at != std::string::npos;
       at = haystack.find(needle, at + 1)) {
    const auto end = at + needle.size();
    if ((at == 0 || !identifier(haystack[at - 1])) &&
        (end >= haystack.size() || !identifier(haystack[end]))) {
      return true;
    }
  }
  return false;
}

std::string TextAt(const arrow::RecordBatch& batch, int column, int64_t row) {
  auto scalar = batch.column(column)->GetScalar(row);
  return scalar.ok() && (*scalar)->is_valid ? (*scalar)->ToString() : std::string();
}

}  // namespace

std::optional<std::string> AccessControlStatementKind(std::string_view sql) {
  const std::string text(sql);
  if (!LooksLikeRbacStatement(text)) return std::nullopt;
  auto parsed = ParseRbacStatement(text);
  if (!parsed.ok() || std::holds_alternative<ShowGrantsCommand>(*parsed) ||
      std::holds_alternative<AlterOwnerCommand>(*parsed)) {
    return std::nullopt;  // DuckDB reports what it makes of it
  }
  return CommandKind(*parsed);
}

// ---- AccessControl ----

AccessControl::AccessControl(const Options& options) {
  RbacEngine::Options engine_options;
  engine_options.store_uri = options.store_uri;
  engine_options.administrator_name = options.administrator_name;
  engine_options.policy_service_url = options.policy_service_url;
  engine_options.policy_service_timeout = options.policy_service_timeout;
  engine_options.poll_interval = options.poll_interval;
  auto engine = RbacEngine::Create(engine_options);
  if (!engine.ok()) throw std::runtime_error(engine.status().message());
  engine_ = *engine;
  seen_ = engine_->snapshot();
  engine_->SetSnapshotListener([this](const Snapshot& snapshot) { OnSnapshot(snapshot); });
}

AccessControl::~AccessControl() {
  if (engine_) engine_->SetSnapshotListener(nullptr);
}

const std::string& AccessControl::store_description() const { return engine_->store_description(); }

void AccessControl::InstallEnforcement(duckdb_v2_instance_handle instance) {
  // DuckDB 2.0 lets optimizer and planner extensions register on an open instance.
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  RegisterRbacEnforcement(dd::DBConfig::GetConfig(database));
}

void AccessControl::SetScopedTokenSigner(ScopedTokenSigner signer) { signer_ = std::move(signer); }

std::optional<AccessControl::SignIn> AccessControl::AuthenticatePassword(
    std::string_view user_name, std::string_view password) const {
  auto principal = engine_->AuthenticatePassword(std::string(user_name), std::string(password));
  if (!principal.ok()) return std::nullopt;
  const UserRecord* user = engine_->snapshot()->FindUserById(principal->user_id);
  if (user == nullptr) return std::nullopt;
  return SignIn{user->name, Fingerprint(user->password_hash)};
}

void AccessControl::RequireStoreUser(const std::string& user_name) const {
  auto principal = engine_->PrincipalForUser(user_name);
  if (!principal.ok()) throw Error(ErrorCode::kInvalidCredentials, principal.status().message());
}

void AccessControl::CheckToken(const Identity& who) const {
  if (who.superuser) return;
  auto snapshot = engine_->snapshot();
  const UserRecord* user = snapshot->FindUserByName(who.user_name);
  if (user == nullptr || !user->enabled) {
    // "is not defined on this server" / "is disabled", as at sign-in.
    auto refused = engine_->PrincipalForUser(who.user_name);
    throw Error(ErrorCode::kInvalidToken, refused.ok() ? "user " + who.user_name + " is disabled"
                                                       : refused.status().message());
  }
  if (!who.credential.empty() && Fingerprint(user->password_hash) != who.credential) {
    throw Error(ErrorCode::kInvalidToken, "the password of user " + user->name +
                                              " changed since this access token was issued; "
                                              "sign in again");
  }
  if (!who.scope_id.empty() && !snapshot->ValidScope(who.scope_id, user->id, NowSeconds())) {
    throw Error(ErrorCode::kInvalidToken, "this scoped token was revoked or has expired");
  }
}

std::shared_ptr<SessionGuard> AccessControl::Bind(duckdb_v2_connection_handle connection,
                                                  Identity who) {
  auto snapshot = engine_->snapshot();
  Principal principal;
  std::string credential;
  std::string tenant;
  if (who.superuser) {
    principal = Principal::Superuser(who.user_name);
  } else {
    auto found = engine_->PrincipalForUser(who.user_name);
    if (!found.ok()) throw Error(ErrorCode::kPermissionDenied, found.status().message(), "42501");
    principal = *found;
    if (const UserRecord* user = snapshot->FindUserById(principal.user_id)) {
      credential = user->password_hash.value_or("");
    }
    tenant = snapshot->TenantOfPrincipal(principal);
    if (!who.scope_id.empty()) {
      principal.scope = snapshot->ValidScope(who.scope_id, principal.user_id, NowSeconds());
      if (!principal.scope) {
        throw Error(ErrorCode::kInvalidToken, "this scoped token was revoked or has expired");
      }
    }
    principal.idp_issuer = who.idp_issuer;
    principal.idp_groups = who.idp_groups;
    // The external policy service may refuse the session.
    if (const auto* service = engine_->policy_service()) {
      switch (service->Decide(PolicyInput(*snapshot, principal, "connect"))) {
        case PolicyService::Decision::kAllow:
          break;
        case PolicyService::Decision::kDeny:
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger access control: the access policy service refused the session",
                      "42501");
        case PolicyService::Decision::kNoAnswer:
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger access control: Hardcharger could not get an answer from the "
                      "access policy service, and refuses sessions until it can",
                      "42501");
      }
    }
  }

  // The principal lives in DuckDB's per-connection state, where SQL cannot reach it.
  // Nothing has run on the connection yet.
  AttachRbacSession(ContextOf(connection), engine_, principal);
  std::shared_ptr<SessionGuard> guard(new SessionGuard(*this, connection, who, principal.user_id));
  guard->credential_ = credential;
  guard->tenant_id_ = tenant;
  if (!tenant.empty()) {
    guard->tenant_catalogs_ = TenantCatalogsKey(*snapshot, tenant);
    // A tenant's sessions start in the tenant's default catalog.
    const auto* record = snapshot->FindTenantById(tenant);
    if (record && !record->default_catalog.empty()) {
      RbacSessionState::BypassScope internal(ContextOf(connection));
      try {
        warpduckdb::RunSql(connection, "USE " + Quoted(record->default_catalog));
      } catch (const Error& error) {
        Log(LogLevel::kWarning, "a tenant's default catalog could not be used",
            {{"user", who.user_name}, {"error", error.what()}});
      }
    }
  }
  std::lock_guard lock(sessions_mutex_);
  std::erase_if(sessions_, [](const auto& weak) { return weak.expired(); });
  sessions_.push_back(guard);
  return guard;
}

void AccessControl::OnSnapshot(const Snapshot& snapshot) {
  std::vector<std::shared_ptr<SessionGuard>> live;
  std::shared_ptr<const Snapshot> previous;
  {
    std::lock_guard lock(sessions_mutex_);
    previous = std::move(seen_);
    seen_ = engine_->snapshot();
    for (const auto& weak : sessions_) {
      if (auto guard = weak.lock()) live.push_back(std::move(guard));
    }
  }
  for (const auto& guard : live) {
    if (guard->user_id_.empty()) continue;  // the administrator
    const UserRecord* user = snapshot.FindUserById(guard->user_id_);
    const bool tenant_changed =
        !guard->tenant_id_.empty() &&
        (!snapshot.FindTenantById(guard->tenant_id_) ||
         TenantCatalogsKey(snapshot, guard->tenant_id_) != guard->tenant_catalogs_);
    const bool scope_gone =
        !guard->identity_.scope_id.empty() &&
        !snapshot.ValidScope(guard->identity_.scope_id, guard->user_id_, NowSeconds());
    std::string reason;
    if (!user) {
      reason = "user dropped";
    } else if (!user->enabled) {
      reason = "user disabled";
    } else if (user->password_hash.value_or("") != guard->credential_) {
      reason = "password changed";
    } else if (tenant_changed) {
      reason = "tenant's catalogs changed";
    } else if (scope_gone) {
      reason = "scoped token revoked or expired";
    }
    if (!reason.empty()) {
      guard->End(reason);
      continue;
    }
    // A statement still running was checked against the rights its user had when it
    // started: stop it if any of them is gone. An idle session's next statement is
    // checked against the new grants anyway.
    if (!previous || !guard->busy()) continue;
    auto state = [&]() -> dd::shared_ptr<RbacSessionState> {
      std::lock_guard lock(guard->mutex_);
      return guard->connection_ ? StateOf(guard->connection_) : nullptr;
    }();
    if (state && snapshot.LostRightsSince(*previous, state->principal())) {
      guard->InterruptRunning();
      Audit(LogLevel::kInfo, "stopped a running statement after its user lost access",
            {{"kind", "access_control"},
             {"user", guard->identity_.user_name},
             {"reason", "rights revoked"}});
    }
  }
}

void AccessControl::EndSessionsOf(const std::string& user_id, const std::string& reason) {
  std::vector<std::shared_ptr<SessionGuard>> ending;
  {
    std::lock_guard lock(sessions_mutex_);
    for (const auto& weak : sessions_) {
      auto guard = weak.lock();
      if (guard && guard->user_id_ == user_id) ending.push_back(std::move(guard));
    }
  }
  for (const auto& guard : ending) guard->End(reason);
}

// ---- SessionGuard ----

SessionGuard::SessionGuard(AccessControl& owner, duckdb_v2_connection_handle connection,
                           Identity identity, std::string user_id)
    : owner_(owner),
      identity_(std::move(identity)),
      user_id_(std::move(user_id)),
      connection_(connection),
      busy_(std::make_shared<std::atomic<int>>(0)) {}

SessionGuard::~SessionGuard() = default;

void SessionGuard::Detach() {
  std::lock_guard lock(mutex_);
  connection_ = nullptr;
}

void SessionGuard::RequireLive() const {
  std::lock_guard lock(mutex_);
  if (ended_) {
    throw Error(ErrorCode::kSessionEnded,
                "Hardcharger access control ended this session (" + *ended_ + ")");
  }
  if (connection_ == nullptr) throw Error(ErrorCode::kSessionEnded, "the session has ended");
}

void SessionGuard::End(const std::string& reason) {
  {
    std::lock_guard lock(mutex_);
    if (ended_) return;
    ended_ = reason;
    if (connection_) duckdb_v2_connection_interrupt(connection_, nullptr);
  }
  Audit(LogLevel::kInfo, "ended a session after an access-control change",
        {{"kind", "access_control"}, {"user", identity_.user_name}, {"reason", reason}});
}

void SessionGuard::InterruptRunning() {
  std::lock_guard lock(mutex_);
  if (connection_) duckdb_v2_connection_interrupt(connection_, nullptr);
}

std::shared_ptr<void> SessionGuard::MarkBusy() {
  busy_->fetch_add(1);
  return std::shared_ptr<void>(nullptr, [busy = busy_](void*) { busy->fetch_sub(1); });
}

bool SessionGuard::busy() const { return busy_->load() > 0; }

std::shared_ptr<arrow::RecordBatchReader> SessionGuard::Track(
    std::shared_ptr<arrow::RecordBatchReader> rows, std::shared_ptr<void> busy) {
  return std::make_shared<TrackedReader>(std::move(rows), std::move(busy));
}

void SessionGuard::AuditAllowed() {
  if (identity_.superuser) return;
  auto state = StateOf(connection_);
  if (!state) return;
  const auto accesses = state->TakeAccesses();
  if (accesses.empty()) return;
  std::string objects;
  for (const auto& access : accesses) objects += (objects.empty() ? "" : "; ") + access;
  Audit(LogLevel::kInfo, "access control allowed a statement",
        {{"kind", "access_control"},
         {"status", "allowed"},
         {"user", identity_.user_name},
         {"objects", objects},
         {"active_role", state->active_role_name()}});
}

void SessionGuard::AuditDenied(const Error& error, std::string_view sql) {
  if (std::string_view(error.what()).find("Hardcharger blocked") == std::string_view::npos) return;
  auto state = StateOf(connection_);
  Audit(LogLevel::kWarning, "access control refused a statement",
        {{"kind", "access_control"},
         {"status", "denied"},
         {"user", identity_.user_name},
         {"error", error.what()},
         {"sql", LoggableSql(sql)},
         {"active_role", state ? state->active_role_name() : ""}});
}

Error SessionGuard::ForTenant(const Error& error) const {
  // A tenant's user learns nothing about what other tenants have: an error about a
  // catalog, schema, table or view it cannot see reads exactly like one about one that
  // does not exist, and so does any error that names a catalog it cannot see (DuckDB's
  // suggestions and candidate lists can).
  if (tenant_id_.empty()) return error;
  const std::string message = error.what();
  bool hide = false;
  for (const char* missing : {"does not exist", "cannot see", "access to catalog", "not found",
                              "No catalog", "No schema"}) {
    hide = hide || message.find(missing) != std::string::npos;
  }
  if (!hide) {
    try {
      std::lock_guard lock(mutex_);
      if (connection_ == nullptr) return error;
      auto& context = ContextOf(connection_);
      auto state = StateOf(connection_);
      if (!state) return error;
      const Principal principal = state->principal();
      auto snapshot = owner_.engine_->snapshot();
      for (const auto& database : dd::DatabaseManager::Get(context).GetDatabases()) {
        const std::string name = database->GetName().GetIdentifierName();
        if (!snapshot->CatalogVisible(principal, name) && ContainsWord(message, name)) {
          hide = true;
          break;
        }
      }
    } catch (...) {
      hide = true;  // cannot tell: say nothing
    }
  }
  if (!hide) return error;
  return Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name), "42501");
}

void SessionGuard::CheckBeforeBind(std::string_view sql) {
  std::vector<warpduckdb::SqlStatement> statements;
  try {
    statements = warpduckdb::Parse(connection_, sql);
  } catch (const Error&) {
    return;  // DuckDB reports what it makes of the text when it runs it
  }
  auto engine = owner_.engine_;
  auto snapshot = engine->snapshot();
  auto state = StateOf(connection_);
  if (!state) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this statement: the session has no access-control state",
                "42501");
  }
  const Principal principal = state->principal();
  const std::function<bool(const std::string&)> permitted = [&](const std::string& category) {
    auto privilege = SystemPrivilegeForGatedCategory(category);
    return privilege && snapshot->HasSystemPrivilege(principal, *privilege);
  };
  // A tenant's user may not even name what another tenant has: DuckDB binds some
  // statements without planning them (DESCRIBE, CREATE VIEW), so whether they bind
  // would say whether the name exists.
  NamedTableCallback named_table;
  std::optional<SessionObjectResolver> resolver;
  if (!tenant_id_.empty()) {
    resolver.emplace(*dd::capiv2::Convert(connection_));
    named_table = [&](const std::vector<std::string>& parts) {
      auto hidden = [&](const std::string& catalog) {
        return !snapshot->CatalogVisible(principal, catalog);
      };
      // A leading part that names an attached catalog counts as one, whatever DuckDB
      // makes of the rest (it reads beta.t as beta.main.t when there is no schema beta).
      if (parts.size() >= 2) {
        if (auto catalog = resolver->ResolveCatalog(parts[0]); catalog.ok() && hidden(*catalog)) {
          throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name),
                      "42501");
        }
      }
      auto resolved = resolver->ResolveObject(QualifiedName{parts});
      if (resolved.ok() && hidden(resolved->path.catalog)) {
        throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name),
                    "42501");
      }
    };
  }
  // Table functions are bound (run, in effect) before the plan check sees them: refuse
  // the ones it would refuse first. A table macro is expanded and checked as what it is.
  SessionObjectResolver functions(*dd::capiv2::Convert(connection_));
  const TableFunctionCallback table_function_allowed = [&](const std::vector<std::string>& parts) {
    return IsAllowedTableFunction(parts.back()) || functions.IsTableMacro(QualifiedName{parts});
  };
  for (auto& handle : statements) {
    auto& statement = *dd::capiv2::Convert(handle.get());
    // SHOW GRANTS parses as a SELECT and RESET ROLE as a SET: in a batch they would
    // reach DuckDB as those.
    if (statements.size() > 1 && LooksLikeRbacStatement(statement.query)) {
      throw Error(ErrorCode::kSqlError,
                  "Hardcharger access control: access-control statements run on their own, "
                  "not in a batch with other statements");
    }
    if (auto category = GatedCategory(statement, permitted, named_table, table_function_allowed)) {
      if (auto privilege = SystemPrivilegeForGatedCategory(*category)) {
        throw Error(ErrorCode::kPermissionDenied,
                    "Hardcharger blocked " + *category + ", which needs the " +
                        SystemPrivilegeName(*privilege) + " system privilege. User " +
                        identity_.user_name + " does not hold it (see SHOW GRANTS)",
                    "42501");
      }
      throw Error(ErrorCode::kPermissionDenied,
                  "Hardcharger blocked " + *category +
                      ": under access control it is available to the administrator only",
                  "42501");
    }
  }
}

warpsql::StatementOutcome SessionGuard::RunCommand(const ParsedCommand& parsed) {
  auto engine = owner_.engine_;
  auto state = StateOf(connection_);
  if (!state) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked " + parsed.kind + ": the session has no access-control state",
                "42501");
  }
  const Principal actor =
      identity_.superuser ? Principal::Superuser(identity_.user_name) : state->principal();
  SessionObjectResolver resolver(*dd::capiv2::Convert(connection_));
  auto executed = engine->Execute(parsed.command, actor, resolver);
  if (!executed.ok()) {
    Audit(LogLevel::kWarning, "access-control statement refused",
          {{"kind", "access_control"},
           {"status", "rejected"},
           {"user", identity_.user_name},
           {"statement", parsed.kind},
           {"error", executed.status().message()}});
    throw ForTenant(FromStatus(executed.status()));
  }
  CommandResult result = std::move(*executed);
  if (result.minted) {
    // The token itself goes back once, to the caller; the store keeps only its record.
    if (!owner_.signer_) {
      throw Error(ErrorCode::kExecutionError,
                  "Hardcharger access control: this server cannot sign scoped tokens");
    }
    const std::string token = owner_.signer_(result.minted->user_name, result.minted->token_id,
                                             result.minted->expires_at);
    result.column_names.push_back("token");
    for (auto& row : result.rows) row.push_back(token);
  }
  if (result.active_role_id) {
    const auto* role = engine->snapshot()->FindRoleById(*result.active_role_id);
    state->SetActiveRole(*result.active_role_id, role ? role->name : "");
  }
  for (const auto& user_id : result.terminate_user_ids) {
    owner_.EndSessionsOf(user_id, parsed.kind == "DROP USER" ? "user dropped" : "user disabled");
  }
  Audit(LogLevel::kInfo, "access-control statement",
        {{"kind", "access_control"},
         {"user", identity_.user_name},
         {"statement", parsed.kind},
         {"status", "success"}});
  warpsql::StatementOutcome outcome;
  outcome.rows = Rows(result);
  return outcome;
}

namespace {

// Parses an access-control statement; nullopt when `sql` is ordinary SQL.
std::optional<ParsedCommand> ParseCommand(std::string_view sql) {
  const std::string text(sql);
  if (!LooksLikeRbacStatement(text)) return std::nullopt;
  auto parsed = ParseRbacStatement(text);
  if (!parsed.ok()) throw FromStatus(parsed.status(), "42601");
  return ParsedCommand{std::move(*parsed), CommandKind(*parsed)};
}

}  // namespace

warpsql::StatementOutcome SessionGuard::Execute(
    std::string_view sql, bool has_parameters,
    const std::function<warpsql::StatementOutcome()>& run) {
  RequireLive();
  if (auto command = ParseCommand(sql)) {
    if (has_parameters) {
      throw Error(ErrorCode::kSqlError,
                  "Hardcharger access control: " + command->kind + " takes no bind parameters");
    }
    return RunCommand(*command);
  }
  auto busy = MarkBusy();
  try {
    if (!identity_.superuser) CheckBeforeBind(sql);
    auto outcome = run();
    AuditAllowed();
    if (outcome.rows) outcome.rows = Track(std::move(outcome.rows), std::move(busy));
    return outcome;
  } catch (const Error& error) {
    AuditDenied(error, sql);
    throw ForTenant(error);
  }
}

std::unique_ptr<warpsql::PreparedStatement> SessionGuard::Prepare(
    std::string_view sql,
    const std::function<std::unique_ptr<warpsql::PreparedStatement>()>& prepare) {
  RequireLive();
  if (auto command = ParseCommand(sql)) {
    return std::make_unique<PreparedAccessControl>(std::move(*command));
  }
  auto busy = MarkBusy();
  try {
    if (!identity_.superuser) CheckBeforeBind(sql);
    auto inner = prepare();
    // What the statement creates or drops is recorded once it has run successfully.
    auto state = StateOf(connection_);
    std::vector<PendingOwnership> ownership;
    if (state) ownership = state->TakePreparedOwnership();
    AuditAllowed();
    return std::make_unique<GuardedPrepared>(std::move(inner), std::move(ownership));
  } catch (const Error& error) {
    AuditDenied(error, sql);
    throw ForTenant(error);
  }
}

warpsql::StatementOutcome SessionGuard::ExecutePrepared(
    warpsql::PreparedStatement& statement, bool has_parameters,
    const std::function<warpsql::StatementOutcome(warpsql::PreparedStatement&)>& run) {
  RequireLive();
  if (auto* command = dynamic_cast<PreparedAccessControl*>(&statement)) {
    if (has_parameters) {
      throw Error(ErrorCode::kSqlError, "Hardcharger access control: " + command->command().kind +
                                            " takes no bind parameters");
    }
    return RunCommand(command->command());
  }
  auto* guarded = dynamic_cast<GuardedPrepared*>(&statement);
  if (guarded == nullptr) {
    // Fail closed: every statement this session prepared went through Prepare above.
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this statement: it was not prepared under access control",
                "42501");
  }
  auto busy = MarkBusy();
  try {
    auto outcome = run(guarded->inner());
    auto ownership = guarded->TakeOwnership();
    if (!ownership.empty()) {
      if (auto state = StateOf(connection_)) {
        ApplyOwnership(*owner_.engine_, state->principal(), ownership);
      }
    }
    AuditAllowed();
    if (outcome.rows) outcome.rows = Track(std::move(outcome.rows), std::move(busy));
    return outcome;
  } catch (const Error& error) {
    AuditDenied(error, "(prepared statement)");
    throw ForTenant(error);
  }
}

int64_t SessionGuard::Ingest(const warpsql::IngestTarget& target,
                             const std::function<int64_t()>& run) {
  RequireLive();
  auto busy = MarkBusy();
  try {
    const int64_t rows = run();
    AuditAllowed();
    return rows;
  } catch (const Error& error) {
    AuditDenied(error, "(bulk ingest into " + target.table + ")");
    throw ForTenant(error);
  }
}

std::shared_ptr<arrow::RecordBatchReader> SessionGuard::Metadata(
    warpsql::MetadataKind kind, const warpsql::MetadataFilter& filter,
    const std::function<std::shared_ptr<arrow::RecordBatchReader>(const warpsql::MetadataFilter&)>&
        run) {
  RequireLive();
  auto state = StateOf(connection_);
  if (!state) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this request: the session has no access-control state",
                "42501");
  }
  // Hardcharger's own lookup over DuckDB's catalog views. Names are not protected (as
  // duckdb_tables() shows them), except to a tenant's users, who see only their
  // tenant's catalogs.
  std::shared_ptr<arrow::RecordBatchReader> rows;
  {
    RbacSessionState::BypassScope internal(ContextOf(connection_));
    rows = run(filter);
    if (tenant_id_.empty() || kind == warpsql::MetadataKind::kTableTypes) return rows;
    const Principal principal = state->principal();
    auto snapshot = owner_.engine_->snapshot();
    std::vector<int> catalog_columns = {0};
    if (kind == warpsql::MetadataKind::kImportedKeys ||
        kind == warpsql::MetadataKind::kExportedKeys ||
        kind == warpsql::MetadataKind::kCrossReference) {
      catalog_columns = {0, 4};  // pk_catalog_name, fk_catalog_name
    }
    return FilterRows(std::move(rows), [&](const arrow::RecordBatch& batch, int64_t row) {
      for (int column : catalog_columns) {
        if (!snapshot->CatalogVisible(principal, TextAt(batch, column, row))) return false;
      }
      return true;
    });
  }
}

}  // namespace hardcharger::authz
