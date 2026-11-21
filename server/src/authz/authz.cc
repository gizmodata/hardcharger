#include "authz/authz.hpp"
#include <charconv>
#include <iterator>
#include <set>

#include <arrow/api.h>
#include <arrow/io/memory.h>
#include <arrow/ipc/api.h>
#include <openssl/evp.h>

#include <algorithm>
#include <map>

#include <duckdb.hpp>
#include <duckdb/function/macro_function.hpp>
#include <duckdb/main/attached_database.hpp>
#include <duckdb/parser/qualified_name.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database_manager.hpp>
#include <duckdb/parser/parsed_data/create_type_info.hpp>
#include <duckdb/parser/statement/create_statement.hpp>
#include <duckdb/parser/statement/execute_statement.hpp>
#include <duckdb/parser/statement/explain_statement.hpp>
#include <duckdb/parser/statement/prepare_statement.hpp>

#include "audit.hpp"
#include "authz/audit_fields.hpp"
#include "authz/password.hpp"
#include "authz/ddl.hpp"
#include "authz/ducklake_calls.hpp"
#include "authz/enforcement.hpp"
#include "authz/engine.hpp"
#include "authz/gate.hpp"
#include "authz/introspection.hpp"
#include "authz/policies.hpp"
#include "authz/resolver.hpp"
#include "authz/storage_access.hpp"
#include "duckdb_sql.hpp"
#include "logging.hpp"

namespace hardcharger::authz {

namespace dd = duckdb;
using chargewire::Error;
using chargewire::ErrorCode;

// An access-control statement, parsed: run at Execute, or kept by Prepare.
struct ParsedCommand {
  RbacCommand command;
  std::string kind;  // "GRANT", "CREATE USER", ... for audit records (never the text)
};

namespace {

// Does the statement as written (or as PREPARE or EXPLAIN wraps it) create a type named like
// the temporary enum DuckDB makes for a PIVOT without an IN list? (Not one of the statements
// DuckDB makes of a PIVOT: those come inside its MULTI_STATEMENT.)
bool WritesPivotEnumName(dd::SQLStatement& statement) {
  dd::SQLStatement* current = &statement;
  for (int depth = 0; current && depth < 8; ++depth) {
    if (current->type == dd::StatementType::PREPARE_STATEMENT) {
      current = current->Cast<dd::PrepareStatement>().statement.get();
    } else if (current->type == dd::StatementType::EXPLAIN_STATEMENT) {
      current = current->Cast<dd::ExplainStatement>().stmt.get();
    } else {
      break;
    }
  }
  if (!current || current->type != dd::StatementType::CREATE_STATEMENT) return false;
  auto& create = current->Cast<dd::CreateStatement>();
  if (!create.info || create.info->type != dd::CatalogType::TYPE_ENTRY) return false;
  std::string name = create.info->Cast<dd::CreateTypeInfo>().GetTypeName().GetIdentifierName();
  std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
  return name.starts_with("__pivot_enum_");
}

int64_t NowSeconds() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// What a token carries of a user's password: a fingerprint of its stored hash, so a
// token stops working when the password changes, and the hash itself never leaves.
std::string Fingerprint(const std::optional<std::string>& password_hash) {
  return PasswordFingerprint(password_hash);
}

// An access-control failure as a Chargewire error: refusals are permission_denied, the
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
class GuardedPrepared : public chargewire::PreparedStatement {
 public:
  GuardedPrepared(std::unique_ptr<chargewire::PreparedStatement> inner,
                  std::vector<PendingOwnership> ownership, std::string sql)
      : inner_(std::move(inner)), ownership_(std::move(ownership)), sql_(std::move(sql)) {}
  std::shared_ptr<arrow::Schema> ParameterSchema() override { return inner_->ParameterSchema(); }
  std::shared_ptr<arrow::Schema> ResultSchema() override { return inner_->ResultSchema(); }

  chargewire::PreparedStatement& inner() { return *inner_; }
  const std::string& sql() const { return sql_; }
  std::vector<PendingOwnership> TakeOwnership() { return std::move(ownership_); }
  // A run that failed: the next one records them.
  void GiveBackOwnership(std::vector<PendingOwnership> ownership) { ownership_ = std::move(ownership); }

 private:
  std::unique_ptr<chargewire::PreparedStatement> inner_;
  std::vector<PendingOwnership> ownership_;
  std::string sql_;
};

// A prepared access-control statement: it runs when executed, as it runs when
// prepared would run it twice for clients that prepare and then execute.
class PreparedAccessControl : public chargewire::PreparedStatement {
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

// Tables metadata with schemas (Chargewire §10: table_schema, an Arrow schema as an IPC
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
      const ObjectPath table{TextAt(*batch, 0, row), ParseSchemaPathText(TextAt(*batch, 1, row)), TextAt(*batch, 2, row)};
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
    : catalog_rules_only_(options.catalog_rules_only), read_only_(options.read_only) {
  RbacEngine::Options engine_options;
  // Rules only: an empty store of the process's own, which no statement can change.
  engine_options.store_uri = catalog_rules_only_ ? "memory" : options.store_uri;
  engine_options.administrator_name = options.administrator_name;
  engine_options.certificate_administrators = options.certificate_administrators;
  engine_options.policy_service_url = options.policy_service_url;
  engine_options.policy_service_timeout = options.policy_service_timeout;
  engine_options.poll_interval =
      catalog_rules_only_ || read_only_ ? std::chrono::milliseconds(0) : options.poll_interval;
  engine_options.read_only = read_only_;
  engine_options.record_sign_ins = read_only_ && options.record_sign_ins;
  engine_options.password_policy = {options.password_min_length, options.password_min_character_classes,
                                    options.password_max_age_days, options.password_temporary_max_age_hours};
  password_max_age_days_ = options.password_max_age_days;
  password_temporary_max_age_hours_ = options.password_temporary_max_age_hours;
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
  SignIn out{user->name, Fingerprint(user->password_hash)};
  const auto standing = StandingOf(*engine_->snapshot(), user->id);
  out.password_expired = standing.too_old;
  out.temporary_expired = standing.temporary_expired;
  out.temporary_max_age_hours = password_temporary_max_age_hours_;
  return out;
}

PasswordStanding AccessControl::StandingOf(const Snapshot& snapshot, const std::string& user_id) const {
  const int64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
  return snapshot.PasswordStandingOf(user_id, password_max_age_days_, password_temporary_max_age_hours_, now);
}

arrow::Status AccessControl::AppendLoginEvents(const std::vector<LoginEventRecord>& events) {
  return engine_->AppendLoginEvents(events);
}

arrow::Result<std::vector<LoginEventRecord>> AccessControl::LoginEventsSince(int64_t since_ms) {
  return engine_->LoginEventsSince(since_ms);
}

arrow::Status AccessControl::PruneLoginEvents(int64_t failures_before_ms, int64_t successes_before_ms) {
  return engine_->PruneLoginEvents(failures_before_ms, successes_before_ms);
}

arrow::Result<bool> AccessControl::TakeLoginLock(const std::string& key, const std::string& holder, int64_t now_ms,
                                                 int64_t stale_before_ms) {
  return engine_->TakeLoginLock(key, holder, now_ms, stale_before_ms);
}

arrow::Status AccessControl::ReleaseLoginLock(const std::string& key, const std::string& holder) {
  return engine_->ReleaseLoginLock(key, holder);
}

void AccessControl::SetSignInSummaries(SignInSummaries summaries) { sign_in_summaries_ = std::move(summaries); }

void AccessControl::SetPasswordAttempts(PasswordAttempts attempts) { password_attempts_ = std::move(attempts); }

namespace {

AccessControl::TokenUser MatchTokenUser(RbacEngine& engine, const std::string& user_id,
                                        const ProviderIdentity& identity, bool read_only) {
  if (identity.issuer.empty() || identity.subject.empty()) {
    throw Error(ErrorCode::kInvalidCredentials, "SSO sign-in requires a verified issuer and subject");
  }
  AccessControl::TokenUser out;
  auto snapshot = engine.snapshot();
  auto* user = snapshot->FindUserById(user_id);
  if (user && user->enabled && !user->sso_identity && !read_only) {
    auto linked = engine.LinkOnSignIn(user_id, identity);
    if (!linked.ok()) throw Error(ErrorCode::kInvalidCredentials, linked.status().message());
    if (*linked) {
      Audit(LogLevel::kInfo, "linked an existing user on its first SSO sign-in",
            {{"kind", "access_control"},
             {"status", "success"},
             {"method", "sso"},
             {"user", user->name},
             {"user_id", user_id},
             {"issuer", identity.issuer}});
    }
    snapshot = engine.snapshot();
    user = snapshot->FindUserById(user_id);
  }
  const auto* linked = snapshot->FindUserByIdentity(identity);
  if (!user || !user->enabled || (linked && linked != user) ||
      (user->sso_identity ? *user->sso_identity != identity : !read_only)) {
    throw Error(ErrorCode::kInvalidCredentials, "the sign-in identity does not match an enabled user");
  }
  const auto role = user->attributes.find(kProvisionedRoleAttribute);
  out.readonly = role != user->attributes.end() && role->second == "readonly";
  return out;
}

}  // namespace

AccessControl::TokenUser AccessControl::RequireStoreUser(const std::string& user_name, const std::string& issuer,
                                                         const std::string& subject) const {
  RequireFresh(*engine_);
  auto principal = engine_->PrincipalForUser(user_name);
  if (!principal.ok()) throw Error(ErrorCode::kInvalidCredentials, principal.status().message());
  return MatchTokenUser(*engine_, principal->user_id, {issuer, subject}, read_only_);
}

AccessControl::TokenUser AccessControl::ProvisionStoreUser(const std::string& user_name, const std::string& token_role,
                                                           const std::string& issuer, const std::string& subject) {
  RequireFresh(*engine_);
  bool created = false;
  auto principal =
      engine_->ProvisionUser(user_name, {{kProvisionedRoleAttribute, token_role}}, {issuer, subject}, &created);
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
  if (created && read_only_) {
    // Nothing was written: not an audit record, which says what happened.
    would_provision_ = true;
    Log(LogLevel::kInfo, "a sign-in token would provision its user (read-only check; nothing written)",
        {{"user", user_name}, {"issuer", issuer}, {"role", token_role}});
  } else if (created) {
    Audit(LogLevel::kInfo, "provisioned a user on its first sign-in",
          {{"kind", "access_control"}, {"status", "success"}, {"statement", "CREATE USER"},
           {"method", "token_auto_provision"}, {"user", user_name}, {"issuer", issuer}, {"role", token_role}});
  }
  if (created && read_only_) return {.readonly = token_role == "readonly", .created = true};
  TokenUser out = MatchTokenUser(*engine_, principal->user_id, {issuer, subject}, read_only_);
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
  if (!chargewire::CertificateIdentitiesMatch(user->name, identity)) {
    throw Error(ErrorCode::kUnknownCertificateIdentity,
                "the client certificate's URI identity '" + identity + "' differs from user '" + user->name +
                    "' only in case; URI identities must match a user name exactly");
  }
  return user->name;
}

AccessControl::TokenStanding AccessControl::CheckToken(const Identity& who) const {
  if (who.superuser || catalog_rules_only_) return {};
  RequireFresh(*engine_);
  // Only for a token this server signed and verified (JwtTokenService::Validate), so each
  // refusal says why (Chargewire §4.2's reason): only the token's holder learns it.
  namespace why = chargewire::reason;
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
  return {user->id, snapshot->TenantOf(PrincipalKind::kUser, user->id)};
}

bool AccessControl::PasswordMustChange(const std::string& user_name, const std::string& credential) const {
  if (catalog_rules_only_ || credential.empty()) return false;
  const auto snapshot = engine_->snapshot();
  const UserRecord* user = snapshot->FindUserByName(user_name);
  // Only a session that signed in with the password the user has now.
  if (!user || Fingerprint(user->password_hash) != credential) return false;
  return StandingOf(*snapshot, user->id).must_change();
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
            snapshot->ScopeRefusal(who.scope_id, principal.user_id, NowSeconds()).value_or(chargewire::reason::kTokenRevoked);
        throw Error::InvalidToken(refusal, refusal == chargewire::reason::kTokenRevoked ? "this scoped token was revoked"
                                                                                    : "this scoped token has expired");
      }
    }
    principal.idp_issuer = who.idp_issuer;
    principal.idp_groups = who.idp_groups;
    principal.hidden_catalogs = hidden_catalogs_;
    principal.catalog_access_token = who.catalog_access.has_value();
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
        chargewire_duckdb::RunSql(connection, "USE " + Quoted(record->default_catalog));
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
      reason = *scope_refusal == chargewire::reason::kTokenRevoked ? "scoped token revoked" : "scoped token expired";
    }
    if (!reason.empty()) {
      guard->End(reason);
      continue;
    }
    auto state = [&]() -> dd::shared_ptr<RbacSessionState> {
      std::lock_guard lock(guard->mutex_);
      return guard->connection_ ? StateOf(guard->connection_) : nullptr;
    }();
    // A session acting as System Admin whose user no longer holds SYSTEM ADMIN (revoked,
    // its role dropped, taken out of the role, here or on another replica): its next
    // statement is refused anyway (the session's principal is asked of the grants on every
    // statement), and one still running, which nothing checked, is stopped now.
    if (state && state->asks_system_admin() && !snapshot.HoldsSystemAdmin(state->session_principal())) {
      if (guard->busy() && !state->updating_metadata()) guard->InterruptRunning();
      guard->EndSystemAdmin(std::string(kSystemAdminName) + " no longer held");
      continue;
    }
    // A statement still running was checked against the rights its user had when it
    // started: stop it if any of them is gone. An idle session's next statement is
    // checked against the new grants anyway.
    if (!previous || !guard->busy()) continue;
    // This session's short DDL metadata update is not a running data query to
    // revoke. Other sessions still get interrupted, and subsequent statements
    // check the new snapshot.
    if (state && !state->updating_metadata() && snapshot.LostRightsSince(*previous, state->principal())) {
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
  EndSystemAdmin("session closed");
  std::lock_guard lock(mutex_);
  connection_ = nullptr;
}

namespace {
// The session's access-control state, or null once its connection is gone.
dd::shared_ptr<RbacSessionState> StateIfAttached(std::mutex& mutex, const duckdb_v2_connection_handle& connection) {
  std::lock_guard lock(mutex);
  return connection ? StateOf(connection) : nullptr;
}
}  // namespace

bool SessionGuard::system_admin() const {
  // The administrator's own sessions are not store users', and a session without a store
  // (catalog_access rules alone) has nobody to hold SYSTEM ADMIN.
  if (unrestricted_ || user_id_.empty()) return false;
  auto state = StateIfAttached(mutex_, connection_);
  return state && state->principal().acts_as_system_admin();
}

void SessionGuard::VerifySystemAdmin() const {
  if (owner_.catalog_rules_only_) return;
  auto state = StateIfAttached(mutex_, connection_);
  if (!state) return;
  const auto snapshot = owner_.engine_->snapshot();
  // A trigger body runs as its owner, whoever fires it: an owner's exemption lifts the
  // policies in it, so it is asked of the store like the session's own (any session, the
  // administrator's included, may fire it).
  const bool definer_exempt = snapshot->AnyTriggerOwnerExempt();
  const bool own = !unrestricted_ && !user_id_.empty();
  const bool system_admin = own && state->asks_system_admin();
  // A policy exemption takes away every policy of its kind at once: a session that holds
  // one (as this server last heard) asks the store too, so that a revoke on another replica
  // counts at its next statement. One that holds none has nothing to lose.
  bool exempt = false;
  if (own && !system_admin) {
    Principal principal = state->principal();
    principal.policy_exemptions_unverified = false;
    exempt = snapshot->ExemptFromPolicies(principal, SystemPrivilege::kExemptRowPolicies) ||
             snapshot->ExemptFromPolicies(principal, SystemPrivilege::kExemptMaskingPolicies);
  }
  if (!system_admin && !exempt && !definer_exempt) {
    if (own) state->SetPolicyExemptionsUnverified({});
    state->SetDefinerExemptionsUnverified({});
    return;
  }
  // One round trip (the store's data version; a load when it moved). A change this finds
  // reaches every session here as a poll's would (AccessControl::OnSnapshot).
  const auto refreshed = owner_.engine_->Refresh();
  const std::string error = refreshed.ok() ? std::string() : refreshed.message();
  if (system_admin) state->SetSystemAdminUnverified(error);
  if (own) state->SetPolicyExemptionsUnverified(error);
  state->SetDefinerExemptionsUnverified(error);
}

SessionPolicyContext SessionGuard::PolicyContextNow() const {
  SessionPolicyContext answers;
  if (unrestricted_) {
    answers.all_roles_active = true;
    return answers;
  }
  // catalog_access rules alone: no store, so no roles or attributes.
  if (owner_.catalog_rules_only_) return answers;
  auto state = StateIfAttached(mutex_, connection_);
  if (!state) return answers;
  const Principal principal = state->principal();
  if (principal.superuser) {
    answers.all_roles_active = true;
    return answers;
  }
  // Exactly what a policy sees for this principal (PolicyContext::For).
  auto context = PolicyContext::For(*owner_.engine_->snapshot(), principal);
  answers.active_roles = std::move(context.role_names);
  answers.attributes = std::move(context.attributes);
  return answers;
}

bool SessionPolicyContext::RoleActive(const std::string& name) const {
  return all_roles_active || active_roles.count(FoldIdentifier(name)) > 0;
}

std::optional<std::string> SessionPolicyContext::Attribute(const std::string& name) const {
  auto it = attributes.find(name);
  if (it == attributes.end()) return std::nullopt;
  return it->second;
}

void SessionGuard::RefuseIfSystemAdminLost() const {
  if (unrestricted_ || user_id_.empty()) return;
  auto state = StateIfAttached(mutex_, connection_);
  if (!state || !state->asks_system_admin() || state->principal().superuser) return;
  throw Error(ErrorCode::kPermissionDenied, state->SystemAdminRefusal(), "42501");
}

std::string SessionGuard::active_role() const {
  auto state = StateIfAttached(mutex_, connection_);
  return state ? state->active_role_name() : std::string();
}

void SessionGuard::SetReadOnly() {
  read_only_ = true;
  // No session is read-only and System Admin at once: one that turns read-only goes on as
  // its user, with all of its roles.
  auto state = StateIfAttached(mutex_, connection_);
  if (state && state->asks_system_admin()) {
    state->SetActiveRole("", "");
    EndSystemAdmin("the session became read-only");
  }
}

std::vector<std::pair<std::string, std::string>> SessionGuard::AuditWho(bool as_system_admin) const {
  LogFields fields = {{"kind", "access_control"}, {"user", identity_.user_name}};
  if (!user_id_.empty()) fields.emplace_back("user_id", user_id_);
  if (!identity_.session_id.empty()) fields.emplace_back("session_id", identity_.session_id);
  if (as_system_admin) fields.emplace_back("acting_as", kSystemAdminName);
  return fields;
}

void SessionGuard::EndSystemAdmin(const std::string& reason) {
  if (!system_admin_open_.exchange(false)) return;
  LogFields fields = AuditWho(false);
  fields.emplace_back("status", "ended");
  fields.emplace_back("reason", reason);
  Audit(LogLevel::kInfo, "System Admin elevation ended", std::move(fields));
}

void SessionGuard::AuditAdministrativeCommand(std::string_view command,
                                              std::vector<std::pair<std::string, std::string>> extra) const {
  if (!system_admin()) return;
  LogFields fields = AuditWho(true);
  fields.emplace_back("status", "success");
  fields.emplace_back("statement", std::string(command));
  for (auto& field : extra) fields.push_back(std::move(field));
  Audit(LogLevel::kInfo, "administrative command run as System Admin", std::move(fields));
}

std::string SessionGuard::AdministrativeCommands(std::string_view sql) const {
  // The families the pre-bind gate keeps for the administrator and for holders of a system
  // privilege, asked as if the session held none.
  std::string commands;
  try {
    for (auto& handle : chargewire_duckdb::Parse(connection_, sql)) {
      auto& statement = *dd::capiv2::Convert(handle.get());
      if (auto category = GatedCategory(statement, [](const std::string&) { return false; })) {
        commands += (commands.empty() ? "" : "; ") + *category;
      }
    }
  } catch (const std::exception&) {
    // DuckDB reports what it makes of the text when it runs it.
  }
  return commands;
}

void SessionGuard::AuditSystemAdminStatement(std::string_view sql, const std::string& commands,
                                             const Error* error) const {
  LogFields fields = AuditWho(true);
  fields.emplace_back("status", error ? "failed" : "success");
  // As every log line shows SQL: credential statements whole, credential values masked.
  fields.emplace_back("sql", LoggableSql(sql));
  if (!commands.empty()) fields.emplace_back("administrative", commands);
  if (error) fields.emplace_back("error", LoggableError(sql, error->what()));
  Audit(error ? LogLevel::kWarning : LogLevel::kInfo, "statement run as System Admin", std::move(fields));
}

bool SessionGuard::PasswordMustChange() const { return HeldUntilPasswordChanged().must_change(); }

PasswordStanding SessionGuard::HeldUntilPasswordChanged() const {
  if (unrestricted_ || user_id_.empty() || identity_.credential.empty() || owner_.catalog_rules_only_) return {};
  const auto snapshot = owner_.engine_->snapshot();
  // Only a session that signed in with the password the user has now (a temporary one, or
  // one past password_max_age_days) is held by it.
  const UserRecord* user = snapshot->FindUserById(user_id_);
  if (!user || Fingerprint(user->password_hash) != identity_.credential) return {};
  return owner_.StandingOf(*snapshot, user_id_);
}

void SessionGuard::RefuseUntilPasswordChanged(std::string_view what, std::optional<std::string_view> sql) const {
  const auto held = HeldUntilPasswordChanged();
  if (!held.must_change()) return;
  if (sql && LooksLikeRbacStatement(std::string(*sql))) {
    auto parsed = ParseRbacStatement(std::string(*sql));
    if (parsed.ok()) {
      const auto* change = std::get_if<AlterUserCommand>(&*parsed);
      if (change && change->action == AlterUserCommand::Action::kSetPassword && change->current_password &&
          (change->current_user || IdentifiersEqual(change->name, identity_.user_name))) {
        return;  // the one statement such a session runs
      }
    }
  }
  // (Every such refusal says "must be changed before anything else runs": the web UI and
  // clients look for it.)
  const Error refused(ErrorCode::kPermissionDenied,
                      "Hardcharger blocked " + std::string(what) + ": the password of user " + identity_.user_name +
                          (held.temporary ? " was set by someone else" : " has expired (password_max_age_days)") +
                          " and must be changed before anything else runs: ALTER USER CURRENT_USER WITH PASSWORD "
                          "'<new>' REPLACE '<current>'",
                      "42501");
  LogFields fields = AuditWho(false);
  if (!request_id_.empty()) fields.emplace_back("statement_id", request_id_);
  fields.emplace_back("status", "rejected");
  fields.emplace_back("reason", "password must be changed");
  fields.emplace_back("request", std::string(what));
  if (sql) fields.emplace_back("sql", LoggableSql(*sql));
  Audit(LogLevel::kWarning, "statement refused until the password is changed", std::move(fields));
  throw refused;
}

std::optional<std::string> SessionGuard::SystemAdminRefusal() const {
  const std::string blocked = std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName + ": ";
  if (read_only_) {
    return blocked + "this is a read-only session (its connection asked for read_only), and a read-only session "
                     "never acts as System Admin";
  }
  if (identity_.readonly) {
    return blocked + "this session signed in readonly (its sign-in's role), and a readonly session never acts as "
                     "System Admin";
  }
  if (identity_.catalog_access) {
    return blocked + "this session's sign-in token carries catalog_access rules, and a token limited to some "
                     "catalogs never acts as System Admin";
  }
  return std::nullopt;
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
  const auto snapshot = owner_.engine_->snapshot();
  return authz::AccessTableRows(
      table, *snapshot, principal, user_name, owner_.sign_in_summaries_,
      [&](const std::string& user_id) { return owner_.StandingOf(*snapshot, user_id).must_change(); });
}

void SessionGuard::RequireLive() const {
  std::lock_guard lock(mutex_);
  if (ended_) {
    // Spec §5.6's reason for what access control saw.
    namespace why = chargewire::reason;
    const char* reason = why::kCredentialsChanged;  // password changed, the tenant's catalogs changed
    if (*ended_ == "user dropped" || *ended_ == "user disabled") reason = why::kPrincipalDropped;
    if (*ended_ == "scoped token revoked") reason = why::kTokenRevoked;
    if (*ended_ == "scoped token expired") reason = why::kTokenExpired;
    throw Error::SessionEnded(reason, "Hardcharger access control ended this session (" + *ended_ + ")");
  }
  if (!unrestricted_ && !owner_.catalog_rules_only_) RequireFresh(*owner_.engine_);
  if (connection_ == nullptr) {
    throw Error::SessionEnded(chargewire::reason::kKilled, "the session has ended");
  }
  // A session opened with a scoped token ends with the scope: nothing could run in it any
  // more, whatever token a request on it carries.
  if (scope_expires_at_ != 0 && scope_expires_at_ <= NowSeconds()) {
    throw Error::SessionEnded(chargewire::reason::kTokenExpired,
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
  EndSystemAdmin("session ended: " + reason);
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
  LogFields fields = AuditWho(false);
  fields.insert(fields.end(),
                {{"status", "allowed"}, {"objects", objects}, {"active_role", state->active_role_name()}});
  Audit(LogLevel::kInfo, "access control allowed a statement", std::move(fields));
}

void SessionGuard::AuditDenied(const Error& error, std::string_view sql) {
  if (std::string_view(error.what()).find("Hardcharger blocked") == std::string_view::npos) return;
  auto state = StateOf(connection_);
  LogFields fields = AuditWho(false);
  fields.insert(fields.end(), {{"status", "denied"},
                               {"error", LoggableError(sql, error.what())},
                               {"sql", LoggableSql(sql)},
                               {"active_role", state ? state->active_role_name() : ""}});
  Audit(LogLevel::kWarning, "access control refused a statement", std::move(fields));
}

Error SessionGuard::ForTenant(const Error& error) const {
  // A tenant's user learns nothing about what other tenants have: an error about a
  // catalog, schema, table or view it cannot see reads exactly like one about one that
  // does not exist, and so does any error that names a catalog it cannot see (DuckDB's
  // suggestions and candidate lists can).
  if (unrestricted()) return error;
  const std::string message = error.what();
  // DuckDB binds before the privilege hook. Its spelling suggestions search the
  // whole catalog, including objects absent from this user's filtered metadata.
  // Until the engine offers a visibility-aware candidate callback, omit hints for
  // every restricted session (not only tenants). Keep the original error and SQLSTATE.
  if (tenant_id_.empty()) {
    size_t end = message.size();
    for (const char* marker : {"Did you mean", "Candidate tables:", "Candidate bindings:"}) {
      end = std::min(end, message.find(marker));
    }
    if (end == message.size()) return error;
    auto safe = message.substr(0, end);
    while (!safe.empty() && std::isspace(static_cast<unsigned char>(safe.back()))) safe.pop_back();
    return Error(error.code(), safe, error.sqlstate());
  }
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

void SessionGuard::CheckBeforeBind(std::string_view sql, const boost::json::value& parameters, bool preparing) {
  std::vector<chargewire_duckdb::SqlStatement> statements;
  try {
    statements = chargewire_duckdb::Parse(connection_, sql);
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
  // A session that said SET ROLE SYSTEM ADMIN and no longer holds it runs nothing until it
  // says RESET ROLE: said here, before the gate would name a system privilege it lacks.
  if (principal.asks_system_admin() && !principal.superuser) {
    throw Error(ErrorCode::kPermissionDenied, state->SystemAdminRefusal(), "42501");
  }
  const std::function<bool(const std::string&)> permitted = [&](const std::string& category) {
    auto privilege = SystemPrivilegeForGatedCategory(category);
    return privilege && snapshot->HasSystemPrivilege(principal, *privilege);
  };
  // A tenant's user may not even name what another tenant has: DuckDB binds some
  // statements without planning them (DESCRIBE, CREATE VIEW), so whether they bind
  // would say whether the name exists. And DESCRIBE lists a table's columns, which only
  // a user who may see them all may read that way (Chargewire §9.4).
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
  const TableFunctionCallback table_function_allowed =
      [&](const std::vector<std::string>& parts) -> std::optional<std::vector<PathArgument>> {
    if (IsAllowedTableFunction(parts.back())) return std::vector<PathArgument>{};
    // One EXECUTE is granted on: its declared path arguments are checked here as written,
    // and again by the plan check as bound.
    if (parts.size() == 1) {
      if (auto declared = snapshot->TableFunctionPathArguments(principal, parts.back())) return declared;
    }
    if (functions.IsTableMacro(QualifiedName{parts})) return std::vector<PathArgument>{};
    return std::nullopt;
  };
  // Files, URLs and object storage: checked by the paths the statement writes, before
  // binding reads them (and again, as bound, by the plan check).
  std::optional<StorageAccess> storage_access;
  const StorageCallback storage = [&](const StorageUse& use) {
    if (principal.superuser) return;
    if (!storage_access) storage_access.emplace(*dd::capiv2::Convert(connection_)->context);
    StorageRequest request{use.what, use.write ? StorageMode::kWrite : StorageMode::kRead, use.paths, use.directory,
                           use.own_client};
    if (auto refusal = storage_access->Refusal(*snapshot, {principal}, request, identity_.user_name)) {
      throw Error(ErrorCode::kPermissionDenied, *refusal, "42501");
    }
  };
  const PathParameterCallback paths_of_parameter = [&](const std::string& name) -> std::optional<std::vector<std::string>> {
    // Preparing a direct placeholder cannot open its unknown path. Its actual value
    // must pass this gate on every execution, before DuckDB binds/opens the file.
    if (preparing) return std::vector<std::string>{};
    const boost::json::value* value = nullptr;
    if (parameters.is_object()) {
      for (const auto& item : parameters.as_object()) {
        if (!IdentifiersEqual(std::string(item.key()), name)) continue;
        if (value) return std::nullopt;  // ambiguous names must not authorize a different bind
        value = &item.value();
      }
    } else if (parameters.is_array()) {
      std::size_t position = 0;
      const auto [end, error] = std::from_chars(name.data(), name.data() + name.size(), position);
      if (error == std::errc{} && end == name.data() + name.size() && position > 0 && position <= parameters.as_array().size())
        value = &parameters.as_array()[position - 1];
    }
    // JSON binds are scalars. A list of placeholders is resolved one element at a time.
    if (!value || !value->is_string()) return std::nullopt;
    return std::vector<std::string>{std::string(value->as_string())};
  };
  // What DuckDB evaluates while it binds (gate.hpp): sequences moved by a table function's
  // arguments, and the bodies of the user macros a statement calls, which the plan check sees
  // only after binding has run them.
  BindTimeCallbacks bind_time;
  std::map<std::string, std::vector<std::shared_ptr<dd::MacroFunction>>> macro_definitions;
  if (!principal.superuser) {
    bind_time.macros = [&](const std::vector<std::string>& parts, bool table) {
      std::string key = table ? "table" : "scalar";
      for (const auto& part : parts) key += '\x1f' + part;
      auto found = macro_definitions.find(key);
      if (found != macro_definitions.end()) return found->second;
      auto definitions = functions.MacroDefinitions(QualifiedName{parts}, table);
      macro_definitions.emplace(key, definitions);
      return definitions;
    };
  }
  // Sequences moved while binding: checked here by the rule the plan check applies to the same call
  // (enforcement.cc, CheckSequenceUse). A read-only session's own screen (the SDK's, after this gate)
  // refuses every call DuckDB would evaluate while binding, with read_only's answer: it is left to that.
  if (!principal.superuser && !read_only_) {
    bind_time.sequence = [&](const std::string& function, const std::optional<std::string>& name) {
      const std::string blocked = "Hardcharger blocked " + function + "()";
      const std::string why = ": DuckDB runs it while it binds the statement, before access control checks the plan";
      // A readonly principal's session changes nothing, whatever its rules or grants: nextval()
      // and setval() move the sequence while DuckDB binds, before the readonly check of the plan.
      if (function != "currval") {
        if (auto readonly = ReadOnlyRefusal(ContextOf(connection_))) {
          throw Error(ErrorCode::kPermissionDenied, *readonly, "42501");
        }
      }
      if (!name) {
        throw Error(ErrorCode::kPermissionDenied, blocked + why + ", and its sequence is not named by a string literal",
                    "42501");
      }
      std::vector<std::string> parts;
      try {
        for (const auto& part : dd::QualifiedName::ParseComponents(*name)) parts.push_back(part.GetIdentifierName());
      } catch (const std::exception&) {
        parts.clear();
      }
      arrow::Result<ObjectPath> sequence = arrow::Status::Invalid("no name");
      if (!parts.empty()) sequence = functions.ResolveSequence(QualifiedName{parts});
      if (!sequence.ok()) {
        throw Error(ErrorCode::kPermissionDenied, blocked + why + ", and access control cannot find the sequence it names",
                    "42501");
      }
      if (!snapshot->CatalogVisible(principal, sequence->catalog)) {
        throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name), "42501");
      }
      // A catalog_access principal: its rules (nextval / setval write, currval read; temp included).
      if (principal.catalog_rules) {
        const auto level = CatalogRuleLevel(*principal.catalog_rules, sequence->catalog);
        const auto needed = function == "currval" ? CatalogAccessLevel::kRead : CatalogAccessLevel::kWrite;
        if (level == CatalogAccessLevel::kNone || (needed == CatalogAccessLevel::kWrite && level != needed)) {
          throw Error(ErrorCode::kPermissionDenied, CatalogAccessDenied(sequence->catalog, needed, principal.user_name),
                      "42501");
        }
        return;
      }
      // A store user: USAGE for nextval / currval, DROP SEQUENCE on the schema for setval (which
      // resets the sequence, as dropping and recreating it would). Its own temp sequences are its own.
      if (IdentifiersEqual(sequence->catalog, "temp")) return;
      if (function == "setval") {
        ObjectPath schema = *sequence;
        schema.object_name.clear();
        if (!snapshot->Check(principal, AccessRequest{schema, ObjectType::kSchema, Privilege::kDropSequence, {}}).allowed) {
          throw Error(ErrorCode::kPermissionDenied,
                      blocked + ": user " + principal.user_name + " may not setval() of sequence " + sequence->ToString() +
                          " (it needs DROP SEQUENCE on schema " + schema.ToString() + " or its catalog, or to own the schema)",
                      "42501");
        }
        return;
      }
      if (!snapshot->Check(principal, AccessRequest{*sequence, ObjectType::kSequence, Privilege::kUsage, {}}).allowed) {
        throw Error(ErrorCode::kPermissionDenied,
                    blocked + ": user " + principal.user_name + " has no USAGE privilege on sequence " + sequence->ToString(),
                    "42501");
      }
    };
  }
  // A non-administrator's DuckLake calls (ducklake_snapshots('lake'),
  // CALL lake.merge_adjacent_files()): checked here against the catalog they name, before
  // binding runs them, and remembered for the plan check, which refuses any other DuckLake
  // call (authz/ducklake_calls.hpp). A catalog_access principal's rules decide; a store
  // user needs EXECUTE on the function and, on the catalog, SELECT (inspection) or
  // MAINTAIN (maintenance) granted ON CATALOG.
  std::set<std::pair<std::string, std::string>> ducklake_checked;
  DuckLakeCallCallback ducklake_call;
  if (!principal.superuser) {
    ducklake_call = [&](const TableFunctionCall& call) -> bool {
      std::string name = call.parts.back();
      std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
      std::string function;
      std::optional<std::string> catalog;
      std::optional<DuckLakeAccess> access = DuckLakeFunctionAccess(name);
      if (access) {
        function = name;
        catalog = call.first_literal;
      } else if (IsDuckLakeCatalogMethod(name)) {
        // lake.snapshots(): DuckLake's own macro, if that is what the name finds.
        catalog = functions.DuckLakeMacroCatalog(QualifiedName{call.parts});
        if (!catalog) return false;
        function = "ducklake_" + name;
        access = DuckLakeFunctionAccess(function);
      } else {
        return false;
      }
      auto refuse = [](const std::string& message) {
        throw Error(ErrorCode::kPermissionDenied, message, "42501");
      };
      // A store user's administrator-only and unknown DuckLake functions stay what they
      // were: table functions an EXECUTE grant opens (checked below, as any).
      const bool store_user = !principal.catalog_rules;
      if (store_user && (*access == DuckLakeAccess::kAdministrator || *access == DuckLakeAccess::kUnknown)) {
        return false;
      }
      if (!call.wrapper.empty()) refuse(DuckLakeWrappedRefusal(function, call.wrapper));
      if (*access == DuckLakeAccess::kAdministrator) refuse(DuckLakeAdministratorRefusal(function));
      if (*access == DuckLakeAccess::kUnknown) refuse(DuckLakeUnknownRefusal(function));
      if (!catalog) refuse(DuckLakeComputedCatalogRefusal(function));
      // The catalog as DuckDB finds it (the rules and grants name its own name).
      std::string lake = *catalog;
      if (auto resolved = functions.ResolveCatalog(lake); resolved.ok()) lake = *resolved;
      const auto needed = *access == DuckLakeAccess::kWrite ? CatalogAccessLevel::kWrite : CatalogAccessLevel::kRead;
      if (!snapshot->CatalogVisible(principal, lake)) {
        // The server's own catalogs (and another tenant's) read as missing; a catalog_access
        // refusal says why.
        const bool servers = std::any_of(principal.hidden_catalogs.begin(), principal.hidden_catalogs.end(),
                                         [&](const std::string& c) { return IdentifiersEqual(c, lake); });
        if (servers || store_user) {
          throw Error(ErrorCode::kPermissionDenied, HiddenObjectMessage(identity_.user_name), "42501");
        }
        refuse(CatalogAccessDenied(lake, needed, principal.user_name));
      }
      if (store_user) {
        for (const auto& bound : DuckLakeBoundFunctions(function)) {
          if (!snapshot->TableFunctionPathArguments(principal, bound)) refuse(DuckLakeExecuteRefusal(bound));
        }
        if (!snapshot->HoldsOnCatalog(principal, DuckLakeCatalogPrivilege(*access), lake)) {
          refuse(DuckLakeCatalogPrivilegeRefusal(function + "()", lake, *access, principal.user_name));
        }
      } else if (needed == CatalogAccessLevel::kWrite &&
                 CatalogRuleLevel(*principal.catalog_rules, lake) != CatalogAccessLevel::kWrite) {
        refuse(CatalogAccessDenied(lake, needed, principal.user_name));
      }
      // A readonly principal's session changes nothing, whatever its rules (DuckDB does not
      // count DuckLake's maintenance in FROM as a change).
      if (needed == CatalogAccessLevel::kWrite) {
        if (auto readonly = ReadOnlyRefusal(ContextOf(connection_))) refuse(*readonly);
      }
      for (const auto& bound : DuckLakeBoundFunctions(function)) ducklake_checked.insert({bound, FoldIdentifier(lake)});
      return true;
    };
  }
  for (auto& handle : statements) {
    auto& statement = *dd::capiv2::Convert(handle.get());
    // SHOW GRANTS parses as a SELECT and RESET ROLE as a SET: in a batch they would
    // reach DuckDB as those.
    if (statements.size() > 1 && LooksLikeRbacStatement(statement.query)) {
      throw Error(ErrorCode::kSqlError,
                  "Hardcharger access control: access-control statements run on their own, "
                  "not in a batch with other statements");
    }
    // PIVOT without an IN list makes DuckDB create a temporary enum named __pivot_enum_<id>
    // (one statement of the several it makes of the PIVOT), which the plan check lets a user
    // create. A statement the user writes may not take that name.
    if (WritesPivotEnumName(statement)) {
      throw Error(ErrorCode::kPermissionDenied,
                  "Hardcharger blocked this statement: type names starting with __pivot_enum_ are DuckDB's own, for "
                  "PIVOT", "42501");
    }
    // EXECUTE name runs what PREPARE name checked.
    if (!principal.superuser && statement.type == dd::StatementType::EXECUTE_STATEMENT) {
      const auto prepared =
          state->PreparedDuckLakeCalls(FoldIdentifier(statement.Cast<dd::ExecuteStatement>().name.GetIdentifierName()));
      ducklake_checked.insert(prepared.begin(), prepared.end());
    }
    const auto checked_before = ducklake_checked;
    if (auto category = GatedCategory(statement, permitted, named_table, table_function_allowed, storage,
                                      paths_of_parameter, ducklake_call, bind_time)) {
      if (auto privilege = SystemPrivilegeForGatedCategory(*category)) {
        if (principal.rules_administrator) {
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger blocked " + RulesAdministratorRefusal(*category, *privilege), "42501");
        }
        if (principal.catalog_access_token && !principal.scope) {
          throw Error(ErrorCode::kPermissionDenied,
                      "Hardcharger blocked " + CatalogAccessTokenRefusal(*category, *privilege, identity_.user_name),
                      "42501");
        }
        throw Error(ErrorCode::kPermissionDenied,
                    "Hardcharger blocked " + *category + ", which needs the " +
                        SystemPrivilegeName(*privilege) + " system privilege. User " +
                        identity_.user_name + " does not hold it (see SHOW GRANTS)",
                    "42501");
      }
      if (const auto at = category->find(kInMacro); category->starts_with("the table function ") && at != std::string::npos) {
        // Worded as the plan check words the same refusal once the macro is expanded.
        const std::string name = category->substr(std::string("the table function ").size(),
                                                  at - std::string("the table function ").size());
        throw Error(ErrorCode::kPermissionDenied,
                    "Hardcharger blocked this statement: the table function " + name +
                        " is not available under access control (an administrator can GRANT EXECUTE ON TABLE FUNCTION " +
                        name + " to a role); the macro " + category->substr(at + kInMacro.size()) +
                        " that the statement calls runs it",
                    "42501");
      }
      std::string hint;
      if (category->starts_with("the table function ")) {
        const std::string name = category->substr(std::string("the table function ").size());
        hint = ", or to a role granted EXECUTE ON TABLE FUNCTION " + name;
      }
      throw Error(ErrorCode::kPermissionDenied,
                  "Hardcharger blocked " + *category +
                      ": under access control it is available to the administrator only" + hint,
                  "42501");
    }
    if (!principal.superuser && statement.type == dd::StatementType::PREPARE_STATEMENT) {
      std::set<std::pair<std::string, std::string>> calls;
      std::set_difference(ducklake_checked.begin(), ducklake_checked.end(), checked_before.begin(),
                          checked_before.end(), std::inserter(calls, calls.end()));
      state->SetPreparedDuckLakeCalls(
          FoldIdentifier(statement.Cast<dd::PrepareStatement>().name.GetIdentifierName()), std::move(calls));
    }
  }
  // The calls checked here are the only DuckLake calls the plan check lets run: one the walk
  // did not reach (a macro's body, a form it does not read) is refused there, as bound, and
  // DuckLake's binds change nothing (the maintenance functions act when they execute). A
  // walk of the parse tree, not the statement's text: a table or column list named like a
  // DuckLake function (INSERT INTO s.settings(k, v), CREATE TABLE ducklake_log (...)) is no
  // call.
  if (!principal.superuser) state->SetCheckedDuckLakeCalls(std::move(ducklake_checked));
}

void SessionGuard::CheckReadOnlyCommand(const ParsedCommand& parsed) const {
  if (read_only_ && !parsed.kind.starts_with("SHOW") && parsed.kind != "SET ROLE") {
    throw Error(ErrorCode::kReadOnly, parsed.kind + " is not allowed in a read-only session", "25006");
  }
}

chargewire::StatementOutcome SessionGuard::RunCommand(const ParsedCommand& parsed) {
  CheckReadOnlyCommand(parsed);
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
  // A certificate-bound session's scoped tokens are bound to its certificate (Chargewire §4.4),
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
  auto before = engine->snapshot();
  const auto* set_role = std::get_if<SetRoleCommand>(&parsed.command);
  const bool elevating = set_role != nullptr && set_role->system_admin && !unrestricted_;
  const std::string kind = elevating ? std::string("SET ROLE ") + kSystemAdminName : parsed.kind;
  // What the statement names (never a password or a token): who it is about, and the
  // privilege or membership it changes, with the store's ids for the users and roles.
  // ALTER USER CURRENT_USER is described as the session's own user.
  RbacCommand audited = parsed.command;
  if (auto* own = std::get_if<AlterUserCommand>(&audited); own && own->current_user && !user_id_.empty()) {
    own->current_user = false;
    own->name = identity_.user_name;
  }
  auto described = [&](LogFields fields, const Snapshot& after) {
    for (auto& field : AuditFieldsOf(audited, *before, after)) fields.push_back(std::move(field));
    return fields;
  };
  // Which statement of the session this is (the client's request id, its sql_statements
  // row), and the role SET ROLE narrowed the session to: every access-control record says.
  auto correlated = [&](LogFields fields, const Principal& who) {
    if (!request_id_.empty()) fields.emplace_back("statement_id", request_id_);
    if (!who.active_role_id.empty() && !who.asks_system_admin()) fields.emplace_back("active_role", who.active_role_name);
    return fields;
  };
  Principal session_user = state->session_principal();
  auto refuse = [&](const Error& error, bool as_system_admin) {
    LogFields fields = correlated(AuditWho(as_system_admin), session_user);
    fields.emplace_back("status", "rejected");
    fields.emplace_back("statement", kind);
    fields.emplace_back("error", error.what());
    Audit(LogLevel::kWarning, elevating ? "System Admin elevation refused" : "access-control statement refused",
          described(std::move(fields), *before));
  };
  // SET ROLE and RESET ROLE are the user's own, whatever the session acts as: they are
  // asked of the user's roles. Every other statement runs as what the session acts as now:
  // the user, or, acting as System Admin, the administrator (asked of the grants here, at
  // this statement, and again inside the store transaction of what it changes).
  Principal actor = set_role != nullptr && !unrestricted_ ? state->session_principal() : state->principal();
  const bool as_system_admin = actor.acts_as_system_admin();
  if (set_role == nullptr && actor.asks_system_admin() && !actor.superuser) {
    const Error lost(ErrorCode::kPermissionDenied, state->SystemAdminRefusal(), "42501");
    refuse(lost, false);
    throw lost;
  }
  if (elevating) {
    // The session's own limits are never widened by its user's grants.
    if (auto refusal = SystemAdminRefusal()) {
      const Error refused(ErrorCode::kPermissionDenied, *refusal, "42501");
      refuse(refused, false);
      throw refused;
    }
    // The store's own word, as of now: not what this replica last polled. A store that
    // does not answer elevates nobody.
    if (auto refreshed = engine->Refresh(); !refreshed.ok()) {
      const Error refused(ErrorCode::kPermissionDenied,
                          std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName +
                              ": it could not be checked against the access control store (" + refreshed.message() +
                              ")",
                          "42501");
      refuse(refused, false);
      throw refused;
    }
    before = engine->snapshot();
    // An external policy service may refuse it, as it may refuse a session or a statement.
    if (const auto* service = engine->policy_service()) {
      const auto decision = service->Decide(PolicyInput(*before, actor, "system_admin"));
      if (decision != PolicyService::Decision::kAllow) {
        const Error refused(ErrorCode::kPermissionDenied,
                            std::string("Hardcharger blocked SET ROLE ") + kSystemAdminName +
                                (decision == PolicyService::Decision::kDeny
                                     ? ": the access policy service refused it"
                                     : ": Hardcharger could not get an answer from the access policy service"),
                            "42501");
        refuse(refused, false);
        throw refused;
      }
    }
  }
  // A user's own password change gives the current password: the login guard limits and
  // counts it as it does a sign-in of that user from this session's client address.
  std::unique_ptr<PasswordAttempt> attempt;
  if (const auto* own = std::get_if<AlterUserCommand>(&parsed.command);
      own && own->current_password && owner_.password_attempts_ && !identity_.superuser &&
      !identity_.user_name.empty()) {
    attempt = owner_.password_attempts_(identity_.user_name, identity_.session_id);
    if (attempt && attempt->refused_for_seconds() > 0) {
      const int64_t seconds = attempt->refused_for_seconds();
      const Error refused = Error::RetryAfter(ErrorCode::kRateLimited, std::chrono::seconds(seconds),
                                              "Hardcharger blocked ALTER USER: too many failed sign-in attempts: "
                                              "try again in " + std::to_string(seconds) + " s");
      refuse(refused, false);
      throw refused;
    }
  }
  SessionObjectResolver resolver(*dd::capiv2::Convert(connection_));
  RbacEngine::AuditNotes notes;
  auto executed = engine->Execute(parsed.command, actor, resolver, &notes);
  if (!executed.ok()) {
    if (attempt && IsWrongCurrentPassword(executed.status())) attempt->Failed();
    const Error error = FromStatus(executed.status());
    refuse(error, as_system_admin);
    throw ForTenant(error);
  }
  CommandResult result = std::move(*executed);
  if (result.minted) {
    // The token itself goes back once, to the caller; the store keeps only its record.
    if (!owner_.signer_) {
      throw Error(ErrorCode::kNotImplemented,
                  "Hardcharger access control: this server cannot sign scoped tokens");
    }
    // A certificate-bound minter's tokens are bound to the same certificate (Chargewire
    // §4.4): a scoped token can't carry a session's rights away from its certificate.
    const std::string token = owner_.signer_(result.minted->user_name, result.minted->token_id,
                                             result.minted->expires_at, identity_.certificate_thumbprint);
    result.column_names.push_back("token");
    for (auto& row : result.rows) row.push_back(token);
  }
  const auto after = engine->snapshot();
  if (result.active_role_id) {
    const bool was_asking = state->asks_system_admin();
    if (*result.active_role_id == kSystemAdminRoleId) {
      state->SetSystemAdminUnverified({});  // the store answered just now
      state->SetActiveRole(kSystemAdminRoleId, kSystemAdminName);
    } else {
      const auto* role = after->FindRoleById(*result.active_role_id);
      state->SetActiveRole(*result.active_role_id, role ? role->name : "");
      // RESET ROLE (SET ROLE ALL | NONE), or SET ROLE with one of the user's roles.
      if (was_asking) EndSystemAdmin(set_role != nullptr && set_role->role ? "SET ROLE " + *set_role->role : "RESET ROLE");
    }
  }
  for (const auto& user_id : result.terminate_user_ids) {
    owner_.EndSessionsOf(user_id, parsed.kind == "DROP USER" ? "user dropped" : "user disabled");
  }
  if (elevating) {
    // One record for each time the session starts acting as System Admin (saying it again
    // while it does changes nothing).
    if (!system_admin_open_.exchange(true)) {
      std::string via;
      for (const auto& source : after->SystemAdminSources(actor)) {
        const auto* role = source.empty() ? nullptr : after->FindRoleById(source);
        via += (via.empty() ? "" : ", ") + (source.empty() ? std::string("a grant to the user")
                                                           : "role " + (role ? role->name : source));
      }
      LogFields fields = AuditWho(true);
      fields.emplace_back("status", "success");
      fields.emplace_back("statement", kind);
      fields.emplace_back("held_through", via);
      Audit(LogLevel::kInfo, "System Admin elevation", std::move(fields));
    }
  } else {
    LogFields fields = correlated(AuditWho(as_system_admin), session_user);
    fields.emplace_back("statement", parsed.kind);
    fields.emplace_back("status", "success");
    fields = described(std::move(fields), *after);
    // An account change: what authorized it, and its sanitized state before and after.
    for (auto& note : notes) fields.push_back(std::move(note));
    Audit(LogLevel::kInfo, "access-control statement", std::move(fields));
  }
  chargewire::StatementOutcome outcome;
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

chargewire::StatementOutcome SessionGuard::Execute(
    std::string_view sql, bool has_parameters,
    const std::function<chargewire::StatementOutcome()>& run, const boost::json::value& parameters) {
  VerifySystemAdmin();
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
  // Acting as System Admin as of this statement (asked of the grants now): it passes the
  // gate as the administrator's does, and is recorded with its user's name. The plan check
  // asks again when DuckDB binds it.
  const bool acting = system_admin();
  const std::string commands = acting ? AdministrativeCommands(sql) : std::string();
  try {
    if (!unrestricted_ && !acting) CheckBeforeBind(sql, parameters);
    auto outcome = run();
    AuditAllowed();
    if (acting) AuditSystemAdminStatement(sql, commands, nullptr);
    if (outcome.rows) outcome.rows = Track(std::move(outcome.rows), std::move(busy));
    return outcome;
  } catch (const Error& error) {
    AuditDenied(error, sql);
    if (acting) AuditSystemAdminStatement(sql, commands, &error);
    throw ForTenant(error);
  }
}

std::unique_ptr<chargewire::PreparedStatement> SessionGuard::Prepare(
    std::string_view sql,
    const std::function<std::unique_ptr<chargewire::PreparedStatement>()>& prepare) {
  VerifySystemAdmin();
  RequireLive();
  if (!owner_.catalog_rules_only_) {
    if (auto command = ParseCommand(sql)) {
      CheckReadOnlyCommand(*command);
      return std::make_unique<PreparedAccessControl>(std::move(*command));
    }
  }
  auto busy = MarkBusy();
  try {
    if (!unrestricted_ && !system_admin()) CheckBeforeBind(sql, {}, true);
    auto inner = prepare();
    // What the statement creates or drops is recorded once it has run successfully.
    auto state = StateOf(connection_);
    std::vector<PendingOwnership> ownership;
    if (state) ownership = state->TakePreparedOwnership();
    AuditAllowed();
    return std::make_unique<GuardedPrepared>(std::move(inner), std::move(ownership), std::string(sql));
  } catch (const Error& error) {
    AuditDenied(error, sql);
    throw ForTenant(error);
  }
}

chargewire::StatementOutcome SessionGuard::ExecutePrepared(
    chargewire::PreparedStatement& statement, bool has_parameters,
    const std::function<chargewire::StatementOutcome(chargewire::PreparedStatement&)>& run, const boost::json::value& parameters) {
  VerifySystemAdmin();
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
  // As Execute: whether the session acts as System Admin is asked now, at this execution,
  // whatever it acted as when the statement was prepared.
  const bool acting = system_admin();
  const std::string commands = acting ? AdministrativeCommands(guarded->sql()) : std::string();
  try {
    if (!unrestricted_ && !acting) CheckBeforeBind(guarded->sql(), parameters);
    // Its first successful run records who owns what it creates: before it runs, so that
    // nothing it creates can be left without an owner (put back if the run fails).
    auto ownership = guarded->TakeOwnership();
    // Names may have become occupied since PREPARE. Leave them to the execution's
    // rebind: neither an IF NOT EXISTS no-op nor a failing rename may overwrite
    // the current object's access-control records using an old plan's metadata.
    SessionObjectResolver resolver(*dd::capiv2::Convert(connection_));
    std::erase_if(ownership, [&](const PendingOwnership& change) {
      if (change.kind == PendingOwnership::Kind::kRename)
        return resolver.ObjectExists(change.destination, change.type).value_or(true);
      return change.fresh && resolver.ObjectExists(change.object, change.type).value_or(true);
    });
    auto state = ownership.empty() ? nullptr : StateOf(connection_);
    std::vector<RecordedOwnership> recorded;
    if (state) {
      auto status = state->RecordOwnership(ownership, recorded);
      if (!status.ok()) {
        guarded->GiveBackOwnership(std::move(ownership));
        throw Error(ErrorCode::kExecutionError,
                    "Hardcharger did not run this statement: the owner of what it creates could not be "
                    "recorded (" + status.message() + ")");
      }
    }
    chargewire::StatementOutcome outcome;
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
    if (state) state->CompleteOwnership(ContextOf(connection_), ownership, recorded);
    AuditAllowed();
    if (acting) AuditSystemAdminStatement(guarded->sql(), commands, nullptr);
    if (outcome.rows) outcome.rows = Track(std::move(outcome.rows), std::move(busy));
    return outcome;
  } catch (const Error& error) {
    AuditDenied(error, "(prepared statement)");
    if (acting) AuditSystemAdminStatement(guarded->sql(), commands, &error);
    throw ForTenant(error);
  }
}

int64_t SessionGuard::Ingest(const chargewire::IngestTarget& target,
                             const std::function<int64_t()>& run) {
  VerifySystemAdmin();
  RequireLive();
  auto busy = MarkBusy();
  const bool acting = system_admin();
  const std::string what = "(bulk ingest into " + target.table + ")";
  try {
    // A session that asked for SYSTEM ADMIN and does not act as one ingests nothing (said
    // here: an ingest passes no gate that would say it).
    if (auto state = StateOf(connection_); state && !acting && state->asks_system_admin()) {
      throw Error(ErrorCode::kPermissionDenied, state->SystemAdminRefusal(), "42501");
    }
    const int64_t rows = run();
    AuditAllowed();
    if (acting) AuditSystemAdminStatement(what, {}, nullptr);
    return rows;
  } catch (const Error& error) {
    AuditDenied(error, what);
    if (acting) AuditSystemAdminStatement(what, {}, &error);
    throw ForTenant(error);
  }
}

std::shared_ptr<arrow::RecordBatchReader> SessionGuard::Metadata(
    chargewire::MetadataKind kind, const chargewire::MetadataFilter& filter,
    const std::function<std::shared_ptr<arrow::RecordBatchReader>(const chargewire::MetadataFilter&)>&
        run,
    const std::function<std::shared_ptr<arrow::RecordBatchReader>(std::shared_ptr<arrow::RecordBatchReader>)>&
        describe) {
  VerifySystemAdmin();
  RequireLive();
  auto state = StateOf(connection_);
  if (!state) {
    throw Error(ErrorCode::kPermissionDenied,
                "Hardcharger blocked this request: the session has no access-control state",
                "42501");
  }
  // Hardcharger's own lookup over DuckDB's catalog views, run unchecked; then only what
  // the principal may access is kept (Chargewire §9.4), by the snapshot's listing rules,
  // which ask the same question SQL does.
  using Kind = chargewire::MetadataKind;
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
  // The object a row names from its catalog, schema and table columns (the schema is
  // written as its path: a nested schema's parts, dotted; Chargewire spec §10).
  auto object_at = [](const arrow::RecordBatch& batch, int64_t row, int first) {
    return ObjectPath{TextAt(batch, first, row), ParseSchemaPathText(TextAt(batch, first + 1, row)),
                      TextAt(batch, first + 2, row)};
  };
  if (kind == Kind::kDbSchemas) {
    return FilterRows(run(filter), [&](const arrow::RecordBatch& batch, int64_t row) {
      return snapshot->SchemaListed(principal,
                                    ObjectPath{TextAt(batch, 0, row), ParseSchemaPathText(TextAt(batch, 1, row)), {}});
    });
  }
  if (kind == Kind::kTables) {
    // Listed tables only, and only then their schemas: an unlisted table is never described.
    chargewire::MetadataFilter names_only = filter;
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
