#include "authz/authz.hpp"

#include <arrow/api.h>
#include <arrow/io/memory.h>
#include <arrow/ipc/api.h>
#include <openssl/evp.h>

#include <algorithm>

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
#include "authz/introspection.hpp"
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
        throw Error(ErrorCode::kInternalError, "could not build the result");
      }
    }
    std::shared_ptr<arrow::Array> array;
    if (!builder.Finish(&array).ok())
      throw Error(ErrorCode::kInternalError, "could not build the result");
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
  // A run that failed: the next one records them.
  void GiveBackOwnership(std::vector<PendingOwnership> ownership) { ownership_ = std::move(ownership); }

 private:
  std::unique_ptr<warpsql::PreparedStatement> inner_;
  std::vector<PendingOwnership> ownership_;
};

// A prepared access-control statement: it runs when executed, as it runs when
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

// Tables metadata with schemas (Warp SQL §10: table_schema, an Arrow schema as an IPC
// message, in column 4): each table's schema with only the columns `listed` keeps.
std::shared_ptr<arrow::RecordBatchReader> ListedColumnsOnly(
    std::shared_ptr<arrow::RecordBatchReader> tables,
    const std::function<bool(const ObjectPath&, const std::string&)>& listed) {
  constexpr int kTableSchema = 4;
  auto fail = [](const arrow::Status& status) { throw Error(ErrorCode::kExecutionError, status.message()); };
  auto schema = tables->schema();
  arrow::RecordBatchVector out;
  for (;;) {
    auto next = tables->Next();
    if (!next.ok()) fail(next.status());
    auto batch = std::move(next).ValueUnsafe();
    if (batch == nullptr) break;
    const auto& schemas = static_cast<const arrow::BinaryArray&>(*batch->column(kTableSchema));
    arrow::BinaryBuilder rebuilt;
    for (int64_t row = 0; row < batch->num_rows(); row++) {
      const std::string_view bytes = schemas.GetView(row);
      arrow::io::BufferReader reader(std::make_shared<arrow::Buffer>(
          reinterpret_cast<const uint8_t*>(bytes.data()), static_cast<int64_t>(bytes.size())));
      arrow::ipc::DictionaryMemo memo;
      auto table_schema = arrow::ipc::ReadSchema(&reader, &memo);
      if (!table_schema.ok()) fail(table_schema.status());
      const ObjectPath table{TextAt(*batch, 0, row), {TextAt(*batch, 1, row)}, TextAt(*batch, 2, row)};
      arrow::FieldVector kept;
      for (const auto& field : (*table_schema)->fields()) {
        if (listed(table, field->name())) kept.push_back(field);
      }
      arrow::Status appended;
      if (kept.size() == static_cast<std::size_t>((*table_schema)->num_fields())) {
        appended = rebuilt.Append(bytes);
      } else {
        auto serialized = arrow::ipc::SerializeSchema(*arrow::schema(kept, (*table_schema)->metadata()));
        if (!serialized.ok()) fail(serialized.status());
        appended = rebuilt.Append((*serialized)->data(), (*serialized)->size());
      }
      if (!appended.ok()) fail(appended);
    }
    std::shared_ptr<arrow::Array> column;
    if (auto finished = rebuilt.Finish(&column); !finished.ok()) fail(finished);
    auto replaced = batch->SetColumn(kTableSchema, schema->field(kTableSchema), column);
    if (!replaced.ok()) fail(replaced.status());
    out.push_back(*replaced);
  }
  auto reader = arrow::RecordBatchReader::Make(std::move(out), schema);
  if (!reader.ok()) fail(reader.status());
  return *reader;
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

bool LooksLikeAccessControlStatement(std::string_view sql) { return LooksLikeRbacStatement(std::string(sql)); }

// ---- AccessControl ----

AccessControl::AccessControl(const Options& options)
    : catalog_rules_only_(options.catalog_rules_only) {
  RbacEngine::Options engine_options;
  // Rules only: an empty store of the process's own, which no statement can change.
  engine_options.store_uri = catalog_rules_only_ ? "memory" : options.store_uri;
  engine_options.administrator_name = options.administrator_name;
  engine_options.certificate_administrators = options.certificate_administrators;
  engine_options.policy_service_url = options.policy_service_url;
  engine_options.policy_service_timeout = options.policy_service_timeout;
  engine_options.poll_interval = catalog_rules_only_ ? std::chrono::milliseconds(0) : options.poll_interval;
  engine_options.store_timeout = options.store_timeout;
  if (!catalog_rules_only_) {
    engine_options.cluster_id = options.cluster_id;
    engine_options.store_shared = options.store_shared;
    engine_options.max_snapshot_age = options.max_snapshot_age;
  }
  auto engine = RbacEngine::Create(engine_options);
  if (!engine.ok()) throw std::runtime_error(engine.status().message());
  engine_ = *engine;
  seen_ = engine_->snapshot();
  engine_->SetSnapshotListener([this](const Snapshot& snapshot) { OnSnapshot(snapshot); });
}

AccessControl::~AccessControl() {
  if (engine_) engine_->SetSnapshotListener(nullptr);
  Shutdown();
}

void AccessControl::Shutdown() {
  if (engine_) engine_->Stop();
}

const std::string& AccessControl::store_description() const { return engine_->store_description(); }

const std::string& AccessControl::store_file() const { return engine_->store_file(); }

const std::string& AccessControl::store_cluster_id() const { return engine_->store_cluster_id(); }

AccessControl::StoreHealth AccessControl::store_health() const {
  return {engine_->store_up(), engine_->SnapshotAgeSeconds()};
}

namespace {
// Store users are refused once the snapshot is older than rbac_max_snapshot_age_seconds.
void RequireFresh(const RbacEngine& engine) {
  if (auto fresh = engine.RequireFreshSnapshot(); !fresh.ok()) {
    throw Error(ErrorCode::kPermissionDenied, fresh.message(), "42501");
  }
}
}  // namespace

void AccessControl::InstallEnforcement(duckdb_v2_instance_handle instance) {
  // DuckDB 2.0 lets optimizer and planner extensions register on an open instance.
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  RegisterRbacEnforcement(dd::DBConfig::GetConfig(database));
}

void AccessControl::SetScopedTokenSigner(ScopedTokenSigner signer) { signer_ = std::move(signer); }

void AccessControl::SetHiddenCatalogs(std::vector<std::string> catalogs) { hidden_catalogs_ = std::move(catalogs); }

std::optional<AccessControl::SignIn> AccessControl::AuthenticatePassword(
    std::string_view user_name, std::string_view password) const {
  if (!catalog_rules_only_) RequireFresh(*engine_);
  auto principal = engine_->AuthenticatePassword(std::string(user_name), std::string(password));
  if (!principal.ok()) return std::nullopt;
  const UserRecord* user = engine_->snapshot()->FindUserById(principal->user_id);
  if (user == nullptr) return std::nullopt;
  return SignIn{user->name, Fingerprint(user->password_hash)};
}

namespace {

AccessControl::TokenUser TokenUserOf(const Snapshot& snapshot, const std::string& user_id) {
  AccessControl::TokenUser out;
  if (const UserRecord* user = snapshot.FindUserById(user_id)) {
    const auto role = user->attributes.find(kProvisionedRoleAttribute);
    out.readonly = role != user->attributes.end() && role->second == "readonly";
  }
  return out;
}

}  // namespace

AccessControl::TokenUser AccessControl::RequireStoreUser(const std::string& user_name) const {
  RequireFresh(*engine_);
  auto principal = engine_->PrincipalForUser(user_name);
  if (!principal.ok()) throw Error(ErrorCode::kInvalidCredentials, principal.status().message());
  return TokenUserOf(*engine_->snapshot(), principal->user_id);
}

AccessControl::TokenUser AccessControl::ProvisionStoreUser(const std::string& user_name,
                                                           const std::string& token_role,
                                                           const std::string& issuer) {
  RequireFresh(*engine_);
  bool created = false;
  auto principal = engine_->ProvisionUser(user_name, {{kProvisionedRoleAttribute, token_role}}, &created);
  if (!principal.ok()) {
    // A user that exists (a disabled one) was refused as any sign-in is; one that couldn't
    // be created is an access-control refusal.
    if (engine_->snapshot()->FindUserByName(user_name) == nullptr) {
      Audit(LogLevel::kWarning, "refused to provision a user from a sign-in token",
            {{"kind", "access_control"}, {"status", "rejected"}, {"statement", "CREATE USER"},
             {"method", "token_auto_provision"}, {"user", user_name}, {"issuer", issuer},
             {"error", principal.status().message()}});
    }
    throw Error(ErrorCode::kInvalidCredentials, principal.status().message());
  }
  if (created) {
    Audit(LogLevel::kInfo, "provisioned a user on its first sign-in",
          {{"kind", "access_control"}, {"status", "success"}, {"statement", "CREATE USER"},
           {"method", "token_auto_provision"}, {"user", user_name}, {"issuer", issuer}, {"role", token_role}});
  }
  TokenUser out = TokenUserOf(*engine_->snapshot(), principal->user_id);
  out.created = created;
  return out;
}

std::optional<std::string> AccessControl::CertificateUser(const std::string& identity) const {
  if (catalog_rules_only_) return std::nullopt;
  RequireFresh(*engine_);
  const auto snapshot = engine_->snapshot();  // keeps `user` alive
  const UserRecord* user = snapshot->FindUserByName(identity);
  if (user == nullptr || !user->enabled) return std::nullopt;
  // User names compare ignoring ASCII case, but a URI identity (a SPIFFE ID, whose path is
  // case-sensitive) names that user only when it is spelled exactly the same.
  if (!warpsql::CertificateIdentitiesMatch(user->name, identity)) {
    throw Error(ErrorCode::kUnknownCertificateIdentity,
                "the client certificate's URI identity '" + identity + "' differs from user '" + user->name +
                    "' only in case; URI identities must match a user name exactly");
  }
  return user->name;
}

void AccessControl::CheckToken(const Identity& who) const {
  if (who.superuser || catalog_rules_only_) return;
  RequireFresh(*engine_);
  // Only for a token this server signed and verified (JwtTokenService::Validate), so each
  // refusal says why (Warp SQL §4.2's reason): only the token's holder learns it.
  namespace why = warpsql::reason;
  auto snapshot = engine_->snapshot();
  const UserRecord* user = snapshot->FindUserByName(who.user_name);
  if (user == nullptr || !user->enabled) {
    // "is not defined on this server" / "is disabled", as at sign-in.
    auto refused = engine_->PrincipalForUser(who.user_name);
    throw Error::InvalidToken(why::kPrincipalDropped, refused.ok() ? "user " + who.user_name + " is disabled"
                                                                   : refused.status().message());
  }
  if (!who.credential.empty() && Fingerprint(user->password_hash) != who.credential) {
    throw Error::InvalidToken(why::kCredentialsChanged, "the password of user " + user->name +
                                                            " changed since this access token was issued; "
                                                            "sign in again");
  }
  if (!who.scope_id.empty()) {
    if (auto refusal = snapshot->ScopeRefusal(who.scope_id, user->id, NowSeconds())) {
      throw Error::InvalidToken(*refusal, *refusal == why::kTokenRevoked ? "this scoped token was revoked"
                                                                         : "this scoped token has expired");
    }
  }
}

std::shared_ptr<SessionGuard> AccessControl::Bind(duckdb_v2_connection_handle connection,
                                                  Identity who) {
  auto snapshot = engine_->snapshot();
  Principal principal;
  std::string credential;
  std::string tenant;
  // Rules only: a session without rules is the administrator, or another user who
  // reaches every catalog but the server's own, with a non-administrator's limits
  // (every plan checked: no files, secrets, extensions, ...).
  if (catalog_rules_only_ && !who.catalog_access && !who.superuser) {
    who.catalog_access = std::vector<CatalogAccessRule>{{"*", "write"}};
  }
  // A token's catalog_access rules only ever narrow: they restrict an administrator's
  // token (whatever the role, as GizmoSQL 1.x applies them), and under access control
  // they grant a store user nothing (its grants decide).
  const bool rules_principal = who.catalog_access && (catalog_rules_only_ || who.superuser);
  if (who.superuser && !who.catalog_access) {
    // The administrator without rules is allowed everything.
    principal = Principal::Superuser(who.user_name);
  } else if (rules_principal) {
    // The rules restrict even the administrator. Unlike GizmoSQL 1.x, a restricted
    // administrator does not read the server's own catalogs (instrumentation, logs): a
    // read of them exposes every session's SQL text, with literals from catalogs its
    // rules deny. They are hidden from it as from a non-administrator (hidden_catalogs
    // below). An administrator without rules still reads them (the superuser path above).
    principal.user_name = who.user_name;
    std::vector<CatalogRule> rules;
    for (const auto& rule : *who.catalog_access) {
      const auto level = rule.access == "write"  ? CatalogAccessLevel::kWrite
                         : rule.access == "read" ? CatalogAccessLevel::kRead
                                                 : CatalogAccessLevel::kNone;
      rules.push_back(CatalogRule{rule.catalog, level});
    }
    principal.catalog_rules = std::move(rules);
    principal.rules_administrator = who.superuser;
    principal.hidden_catalogs = hidden_catalogs_;  // the server's own catalogs, even from a restricted admin
  } else {
    RequireFresh(*engine_);
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
        const auto refusal =
            snapshot->ScopeRefusal(who.scope_id, principal.user_id, NowSeconds()).value_or(warpsql::reason::kTokenRevoked);
        throw Error::InvalidToken(refusal, refusal == warpsql::reason::kTokenRevoked ? "this scoped token was revoked"
                                                                                    : "this scoped token has expired");
      }
    }
    principal.idp_issuer = who.idp_issuer;
    principal.idp_groups = who.idp_groups;
    principal.hidden_catalogs = hidden_catalogs_;
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
  guard->unrestricted_ = principal.superuser;
  guard->credential_ = credential;
  guard->tenant_id_ = tenant;
  if (principal.scope) guard->scope_expires_at_ = principal.scope->expires_at;
  if (!tenant.empty()) {
    guard->tenant_catalogs_ = TenantCatalogsKey(*snapshot, tenant);
    // A tenant's sessions start in the tenant's default catalog.
    const auto* record = snapshot->FindTenantById(tenant);
    if (record) guard->tenant_name_ = record->name;
    if (record && !record->default_catalog.empty()) {
      RbacSessionState::BypassScope internal(ContextOf(connection));
      try {
        warpduckdb::RunSql(connection, "USE " + Quoted(record->default_catalog));
        guard->started_in_tenant_catalog_ = true;
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
    const auto scope_refusal =
        guard->identity_.scope_id.empty()
            ? std::nullopt
            : snapshot.ScopeRefusal(guard->identity_.scope_id, guard->user_id_, NowSeconds());
    std::string reason;
    if (!user) {
      reason = "user dropped";
    } else if (!user->enabled) {
      reason = "user disabled";
    } else if (user->password_hash.value_or("") != guard->credential_) {
      reason = "password changed";
    } else if (tenant_changed) {
      reason = "tenant's catalogs changed";
    } else if (scope_refusal) {
      reason = *scope_refusal == warpsql::reason::kTokenRevoked ? "scoped token revoked" : "scoped token expired";
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

void SessionGuard::RunInternal(const std::function<void()>& run) {
  RbacSessionState::BypassScope internal(ContextOf(connection_));
  run();
}

void SessionGuard::Detach() {
  std::lock_guard lock(mutex_);
  connection_ = nullptr;
}

std::shared_ptr<arrow::RecordBatch> SessionGuard::AccessTableRows(
    AccessTable table, const std::optional<std::string>& user_name) const {
  // catalog_access rules alone: no store, so no users, roles or grants to show.
  if (owner_.catalog_rules_only_) return EmptyAccessTable(table);
  Principal principal;
  {
    std::lock_guard lock(mutex_);
    if (connection_ == nullptr) return EmptyAccessTable(table);
    auto state = StateOf(connection_);
    if (!state) {
      throw Error(ErrorCode::kPermissionDenied,
                  std::string("Hardcharger blocked ") + AccessTableName(table) +
                      "(): the session has no access-control state",
                  "42501");
    }
    principal = state->principal();
  }
  return authz::AccessTableRows(table, *owner_.engine_->snapshot(), principal, user_name);
}

void SessionGuard::RequireLive() const {
  std::lock_guard lock(mutex_);
  if (ended_) {
    // Spec §5.6's reason for what access control saw.
    namespace why = warpsql::reason;
    const char* reason = why::kCredentialsChanged;  // password changed, the tenant's catalogs changed
    if (*ended_ == "user dropped" || *ended_ == "user disabled") reason = why::kPrincipalDropped;
    if (*ended_ == "scoped token revoked") reason = why::kTokenRevoked;
    if (*ended_ == "scoped token expired") reason = why::kTokenExpired;
    throw Error::SessionEnded(reason, "Hardcharger access control ended this session (" + *ended_ + ")");
  }
  if (!unrestricted_ && !owner_.catalog_rules_only_) RequireFresh(*owner_.engine_);
  if (connection_ == nullptr) {
    throw Error::SessionEnded(warpsql::reason::kKilled, "the session has ended");
  }
  // A session opened with a scoped token ends with the scope: nothing could run in it any
  // more, whatever token a request on it carries.
  if (scope_expires_at_ != 0 && scope_expires_at_ <= NowSeconds()) {
    throw Error::SessionEnded(warpsql::reason::kTokenExpired,
                              "Hardcharger access control ended this session (scoped token expired)");
  }
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
  if (unrestricted_) return;
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
  // would say whether the name exists. And DESCRIBE lists a table's columns, which only
  // a user who may see them all may read that way (Warp SQL §9.4).
  NamedTableCallback named_table;
  std::optional<SessionObjectResolver> resolver;
  const bool restricted = snapshot->RestrictsCatalogs(principal);
  if (!principal.superuser) {
    resolver.emplace(*dd::capiv2::Convert(connection_));
    named_table = [&](const std::vector<std::string>& parts, bool described) {
      if (!restricted && !described) return;
      auto hidden = [&](const std::string& catalog) {
        if (snapshot->CatalogVisible(principal, catalog)) return false;
        // A catalog_access refusal says so; only the server's
        // own catalogs read as missing.
        const bool servers = std::any_of(principal.hidden_catalogs.begin(), principal.hidden_catalogs.end(),
                                         [&](const std::string& c) { return IdentifiersEqual(c, catalog); });
        if (principal.catalog_rules && !servers) {
          throw Error(ErrorCode::kPermissionDenied,
                      CatalogAccessDenied(catalog, CatalogAccessLevel::kRead, principal.user_name), "42501");
        }
        return true;
      };
      // A leading part that names an attached catalog counts as one, whatever DuckDB
      // makes of the rest (it reads beta.t as beta.main.t when there is no schema beta).
      if (restricted && parts.size() >= 2) {
        if (auto catalog = resolver->ResolveCatalog(parts[0]); catalog.ok() && hidden(*catalog)) {
          throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name),
                      "42501");
        }
      }
      auto resolved = resolver->ResolveObject(QualifiedName{parts});
      if (!resolved.ok()) return;  // DuckDB reports what it makes of the name
      if (restricted && hidden(resolved->path.catalog)) {
        throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name),
                    "42501");
      }
      if (!described) return;
      const std::string what = (resolved->type == ObjectType::kView ? "view " : "table ") +
                               resolved->path.ToString();
      if (!snapshot->ObjectListed(principal, resolved->path)) {
        throw Error(ErrorCode::kPermissionDenied,
                    "Hardcharger blocked this statement: user " + principal.user_name +
                        " holds no privilege on " + what + ", so it cannot describe it",
                    "42501");
      }
      for (const auto& column : resolved->columns) {
        if (!snapshot->ColumnListed(principal, resolved->path, column)) {
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger blocked this statement: user " + principal.user_name +
                          " may read only some columns of " + what +
                          ", and DESCRIBE shows them all; pragma_table_info('" +
                          resolved->path.ToString() + "') lists the ones it may read",
                      "42501");
        }
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
        if (principal.rules_administrator) {
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger blocked " + RulesAdministratorRefusal(*category, *privilege), "42501");
        }
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
  // An administrator's token with catalog_access rules manages no users, roles or grants:
  // it could make itself a sign-in its rules don't hold.
  if (identity_.superuser && !unrestricted_) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked " + parsed.kind +
                    ": an administrator token with catalog_access rules cannot run access-control statements",
                "42501");
  }
  // A certificate-bound session's scoped tokens are bound to its certificate (Warp SQL §4.4),
  // so one minted FOR USER someone else would be usable only over this certificate: refused,
  // rather than handed to a user who can't use it (or who would then hold this key).
  if (const auto* scoped = std::get_if<ScopedTokenCommand>(&parsed.command);
      scoped && scoped->kind == ScopedTokenCommand::Kind::kCreate && scoped->for_user &&
      !identity_.certificate_thumbprint.empty() && !IdentifiersEqual(*scoped->for_user, identity_.user_name)) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked CREATE SCOPED TOKEN FOR USER " + *scoped->for_user +
                    ": this session signed in with a client certificate, and its scoped tokens are bound to "
                    "that certificate; mint tokens for other users from a password or token sign-in",
                "42501");
  }
  const Principal actor = unrestricted_ ? Principal::Superuser(identity_.user_name) : state->principal();
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
      throw Error(ErrorCode::kNotImplemented,
                  "Hardcharger access control: this server cannot sign scoped tokens");
    }
    // A certificate-bound minter's tokens are bound to the same certificate (Warp SQL
    // §4.4): a scoped token can't carry a session's rights away from its certificate.
    const std::string token = owner_.signer_(result.minted->user_name, result.minted->token_id,
                                             result.minted->expires_at, identity_.certificate_thumbprint);
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
  if (!owner_.catalog_rules_only_) {
    if (auto command = ParseCommand(sql)) {
      if (has_parameters) {
        throw Error(ErrorCode::kSqlError,
                    "Hardcharger access control: " + command->kind + " takes no bind parameters");
      }
      return RunCommand(*command);
    }
  }
  auto busy = MarkBusy();
  try {
    if (!unrestricted_) CheckBeforeBind(sql);
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
  if (!owner_.catalog_rules_only_) {
    if (auto command = ParseCommand(sql)) {
      return std::make_unique<PreparedAccessControl>(std::move(*command));
    }
  }
  auto busy = MarkBusy();
  try {
    if (!unrestricted_) CheckBeforeBind(sql);
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
    // Its first successful run records who owns what it creates: before it runs, so that
    // nothing it creates can be left without an owner (put back if the run fails).
    auto ownership = guarded->TakeOwnership();
    auto state = ownership.empty() ? nullptr : StateOf(connection_);
    std::vector<RecordedOwnership> recorded;
    if (state) {
      auto status = RecordCreatedOwners(*owner_.engine_, state->principal(), ownership, recorded);
      if (!status.ok()) {
        guarded->GiveBackOwnership(std::move(ownership));
        throw Error(ErrorCode::kExecutionError,
                    "Hardcharger did not run this statement: the owner of what it creates could not be "
                    "recorded (" + status.message() + ")");
      }
    }
    warpsql::StatementOutcome outcome;
    try {
      outcome = run(guarded->inner());
    } catch (...) {
      if (state) {
        UndoRecordedOwners(*owner_.engine_, recorded);
        for (auto& o : ownership) o.recorded = false;
        guarded->GiveBackOwnership(std::move(ownership));
      }
      throw;
    }
    if (state) ApplyOwnership(*owner_.engine_, state->principal(), ownership);
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
        run,
    const std::function<std::shared_ptr<arrow::RecordBatchReader>(std::shared_ptr<arrow::RecordBatchReader>)>&
        describe) {
  RequireLive();
  auto state = StateOf(connection_);
  if (!state) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this request: the session has no access-control state",
                "42501");
  }
  // Hardcharger's own lookup over DuckDB's catalog views, run unchecked; then only what
  // the principal may access is kept (Warp SQL §9.4), by the snapshot's listing rules,
  // which ask the same question SQL does.
  using Kind = warpsql::MetadataKind;
  RbacSessionState::BypassScope internal(ContextOf(connection_));
  const Principal principal = state->principal();
  auto snapshot = owner_.engine_->snapshot();
  // Table types and data types name no object: everyone sees all of them.
  if (principal.superuser || kind == Kind::kTableTypes || kind == Kind::kTypeInfo) {
    return run(filter);
  }
  if (kind == Kind::kCatalogs) {
    auto rows = run(filter);
    if (!snapshot->RestrictsCatalogs(principal)) return rows;
    return FilterRows(std::move(rows), [&](const arrow::RecordBatch& batch, int64_t row) {
      return snapshot->CatalogVisible(principal, TextAt(batch, 0, row));
    });
  }
  // The object a row names from its catalog, schema and table columns.
  auto object_at = [](const arrow::RecordBatch& batch, int64_t row, int first) {
    return ObjectPath{TextAt(batch, first, row), {TextAt(batch, first + 1, row)}, TextAt(batch, first + 2, row)};
  };
  if (kind == Kind::kDbSchemas) {
    return FilterRows(run(filter), [&](const arrow::RecordBatch& batch, int64_t row) {
      return snapshot->SchemaListed(principal, ObjectPath{TextAt(batch, 0, row), {TextAt(batch, 1, row)}, {}});
    });
  }
  if (kind == Kind::kTables) {
    // Listed tables only, and only then their schemas: an unlisted table is never described.
    warpsql::MetadataFilter names_only = filter;
    names_only.include_schema = false;
    auto rows = FilterRows(run(names_only), [&](const arrow::RecordBatch& batch, int64_t row) {
      return snapshot->ObjectListed(principal, object_at(batch, row, 0));
    });
    if (!filter.include_schema) return rows;
    return ListedColumnsOnly(describe(std::move(rows)), [&](const ObjectPath& table, const std::string& column) {
      return snapshot->ColumnListed(principal, table, column);
    });
  }
  // Keys: every table and column a row names must be listed (pk_*, then fk_* for foreign keys).
  std::vector<std::pair<int, int>> sides = {{0, 3}};  // (catalog column, column-name column)
  if (kind == Kind::kImportedKeys || kind == Kind::kExportedKeys || kind == Kind::kCrossReference) {
    sides.emplace_back(4, 7);
  }
  return FilterRows(run(filter), [&](const arrow::RecordBatch& batch, int64_t row) {
    for (const auto& [first, column] : sides) {
      const ObjectPath table = object_at(batch, row, first);
      if (!snapshot->ObjectListed(principal, table) ||
          !snapshot->ColumnListed(principal, table, TextAt(batch, column, row))) {
        return false;
      }
    }
    return true;
  });
}

}  // namespace hardcharger::authz
