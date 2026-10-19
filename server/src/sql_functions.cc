#include "sql_functions.hpp"

#include <arrow/api.h>
#include <arrow/c/bridge.h>

#include <boost/json.hpp>

#include <algorithm>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>

#include "duckdb_v2.hpp"
#include "engine.hpp"
#include "instrumentation.hpp"
#include "runtime.hpp"
#include "authz/authz.hpp"
#include "platform.hpp"
#include "settings.hpp"
#include "version.hpp"
#include "warpsql/server.hpp"

namespace hardcharger {

namespace {

using warpduckdb::Call;
using warpduckdb::Str;
using warpsql::Error;
using warpsql::ErrorCode;

using ScalarFunction = warpduckdb::Owned<duckdb_v2_scalar_function_handle, duckdb_v2_scalar_function_destroy>;
using TableFunction = warpduckdb::Owned<duckdb_v2_table_function_handle, duckdb_v2_table_function_destroy>;
using ArrowImporter = warpduckdb::Owned<duckdb_v2_arrow_importer_handle, duckdb_v2_arrow_importer_destroy>;
using Schema = warpduckdb::Owned<duckdb_v2_schema_handle, duckdb_v2_schema_destroy>;

constexpr idx_t kChunkRows = 2048;

// Callbacks run inside DuckDB: errors go through the slot DuckDB hands over.
template <typename Fn>
void Guarded(duckdb_v2_error_info_handle* err, Fn&& fn) {
  try {
    fn();
  } catch (const Error& e) {
    duckdb_v2_error_info_set_code(*err, e.code() == ErrorCode::kPermissionDenied
                                            ? DUCKDB_V2_ERROR_CONFIGURATION_PERMISSION
                                            : DUCKDB_V2_ERROR_INPUT_INVALID);
    const duckdb_v2_str text = Str(e.what());
    duckdb_v2_error_info_set_text(*err, &text);
  } catch (const std::exception& e) {
    duckdb_v2_error_info_set_code(*err, DUCKDB_V2_ERROR_INPUT_INVALID);
    const duckdb_v2_str text = Str(e.what());
    duckdb_v2_error_info_set_text(*err, &text);
  }
}

warpduckdb::LogicalType Type(duckdb_v2_factory_handle factory, std::string_view name) {
  warpduckdb::LogicalType type;
  const duckdb_v2_str text = Str(name);
  Call([&](auto* e) { return duckdb_v2_factory_create_type_from_text(factory, &text, type.out(), e); });
  return type;
}

// ---- Scalar functions of the session context ----

// A value known when the statement is bound: text (or NULL), or a boolean.
struct ContextValue {
  std::optional<std::string> text;
  std::optional<bool> boolean;
};

struct ScalarSpec {
  Runtime* runtime;
  std::function<ContextValue(Runtime&, const Session*)> value;
};

void ScalarBind(duckdb_v2_function_bind_info_handle info, duckdb_v2_scalar_function_bind_info_handle result,
                duckdb_v2_context_handle, duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* user = nullptr;
    Call([&](auto* e) { return duckdb_v2_function_bind_get_user_data(info, &user, e); });
    const auto& spec = *static_cast<ScalarSpec*>(user);
    auto value = std::make_unique<ContextValue>(spec.value(*spec.runtime, CurrentSession()));
    duckdb_v2_opaque data{value.release(), [](void* p) { delete static_cast<ContextValue*>(p); },
                          [](void* a, void* b) {
                            const auto& x = *static_cast<const ContextValue*>(a);
                            const auto& y = *static_cast<const ContextValue*>(b);
                            return x.text == y.text && x.boolean == y.boolean;
                          }};
    Call([&](auto* e) { return duckdb_v2_scalar_function_bind_set_bind_data(result, &data, e); });
  });
}

void ScalarExec(duckdb_v2_scalar_function_exec_info_handle info, duckdb_v2_context_handle context,
                duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* data = nullptr;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_bind_data(info, &data, e); });
    const auto& value = *static_cast<const ContextValue*>(data);
    duckdb_v2_factory_handle factory = nullptr;
    Call([&](auto* e) { return duckdb_v2_context_get_factory(context, &factory, e); });
    warpduckdb::Value out;
    if (value.boolean) {
      Call([&](auto* e) { return duckdb_v2_value_create_bool(factory, *value.boolean, out.out(), e); });
    } else if (value.text) {
      const duckdb_v2_str text = Str(*value.text);
      Call([&](auto* e) { return duckdb_v2_value_create_varchar(factory, &text, out.out(), e); });
    } else {
      auto type = Type(factory, "VARCHAR");
      Call([&](auto* e) { return duckdb_v2_value_create_null(factory, type.get(), out.out(), e); });
    }
    idx_t rows = 0;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_row_count(info, &rows, e); });
    duckdb_v2_vector_handle vector = nullptr;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_result(info, &vector, e); });
    Call([&](auto* e) { return duckdb_v2_vector_make_constant(vector, out.get(), rows, e); });
  });
}

void RegisterScalar(duckdb_v2_connection_handle connection, duckdb_v2_factory_handle factory, Runtime& runtime,
                    const std::string& name, std::string_view type,
                    std::function<ContextValue(Runtime&, const Session*)> value) {
  ScalarFunction function;
  Call([&](auto* e) { return duckdb_v2_scalar_function_create(factory, function.out(), e); });
  const duckdb_v2_str function_name = Str(name);
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_name(function.get(), &function_name, e); });
  duckdb_v2_function_signature_handle signature = nullptr;
  Call([&](auto* e) { return duckdb_v2_scalar_function_get_signature(function.get(), &signature, e); });
  auto return_type = Type(factory, type);
  Call([&](auto* e) { return duckdb_v2_function_signature_set_return_type(signature, return_type.get(), e); });
  // Same answer for the whole statement, but not across statements (a prepared
  // statement may outlive a SET that changes it).
  Call([&](auto* e) {
    return duckdb_v2_scalar_function_set_property(function.get(), DUCKDB_V2_FUNCTION_PROPERTY_STABILITY,
                                                  DUCKDB_V2_FUNCTION_PROPERTY_STABILITY_CONSISTENT_WITHIN_QUERY, e);
  });
  auto spec = std::make_unique<ScalarSpec>(ScalarSpec{&runtime, std::move(value)});
  duckdb_v2_opaque user{spec.release(), [](void* p) { delete static_cast<ScalarSpec*>(p); }, nullptr};
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_user_data(function.get(), &user, e); });
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_bind_callback(function.get(), ScalarBind, e); });
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_exec_callback(function.get(), ScalarExec, e); });
  Call([&](auto* e) { return duckdb_v2_connection_register_scalar_function(connection, function.get(), e); });
}

// ---- Table functions over Arrow batches, built for each execution ----

using TableProvider = std::function<std::shared_ptr<arrow::RecordBatch>(Runtime&, const Session*)>;

struct TableSpec {
  Runtime* runtime;
  TableProvider rows;
};

struct TableBinding {
  Session* session;  // the session that bound the statement (null outside a client session)
};

struct TableScan {
  ArrowArrayStream stream{};
  ArrowImporter importer;
  bool input_finished = false;
  ~TableScan() {
    importer.reset();
    if (stream.release != nullptr) stream.release(&stream);
  }
};

// Opens a scan of `batch`: an Arrow stream over it and the importer that turns it into chunks.
std::unique_ptr<TableScan> OpenScan(const std::shared_ptr<arrow::RecordBatch>& batch,
                                    duckdb_v2_context_handle context) {
  auto reader = arrow::RecordBatchReader::Make({batch}, batch->schema());
  if (!reader.ok()) throw Error(ErrorCode::kExecutionError, reader.status().message());
  auto scan = std::make_unique<TableScan>();
  if (auto status = arrow::ExportRecordBatchReader(*reader, &scan->stream); !status.ok()) {
    throw Error(ErrorCode::kExecutionError, status.message());
  }
  ArrowSchema schema{};
  if (int code = scan->stream.get_schema(&scan->stream, &schema); code != 0) {
    throw Error(ErrorCode::kExecutionError, "cannot read the table's schema");
  }
  try {
    Call([&](auto* e) {
      return duckdb_v2_arrow_importer_create(context, &schema, kChunkRows, scan->importer.out(), e);
    });
  } catch (...) {
    schema.release(&schema);
    throw;
  }
  schema.release(&schema);
  return scan;
}

// Bind only names the columns. The rows are read again for every execution (TableInit): a
// prepared statement is bound once and may run many times, and each run must see the rows
// as they are then, from the start.
void TableBind(duckdb_v2_function_bind_info_handle info, duckdb_v2_table_function_bind_info_handle result,
               duckdb_v2_context_handle context, duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* user = nullptr;
    Call([&](auto* e) { return duckdb_v2_function_bind_get_user_data(info, &user, e); });
    const auto& spec = *static_cast<TableSpec*>(user);
    auto batch = spec.rows(*spec.runtime, CurrentSession());
    auto scan = OpenScan(batch, context);
    Schema columns;
    Call([&](auto* e) { return duckdb_v2_arrow_importer_get_schema(scan->importer.get(), columns.out(), e); });
    idx_t count = 0;
    Call([&](auto* e) { return duckdb_v2_schema_get_count(columns.get(), &count, e); });
    for (idx_t i = 0; i < count; i++) {
      duckdb_v2_identifier_t name{};
      duckdb_v2_logical_type_handle type = nullptr;  // borrowed from the schema: not ours to destroy
      Call([&](auto* e) { return duckdb_v2_schema_get_field(columns.get(), i, &name, &type, e); });
      Call([&](auto* e) { return duckdb_v2_table_function_bind_add_result_column(result, &name, type, e); });
    }
    // Execution runs outside the session's scope (results stream after the engine call
    // returns, on DuckDB's threads): it reads the rows for the session that bound it, which
    // outlives its statements.
    duckdb_v2_opaque data{new TableBinding{CurrentSession()}, [](void* p) { delete static_cast<TableBinding*>(p); },
                          nullptr};
    Call([&](auto* e) { return duckdb_v2_function_bind_set_bind_data(info, &data, e); });
  });
}

void TableInit(duckdb_v2_table_function_init_global_info_handle info, duckdb_v2_context_handle context,
               duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* user = nullptr;
    Call([&](auto* e) { return duckdb_v2_table_function_init_global_get_user_data(info, &user, e); });
    const auto& spec = *static_cast<TableSpec*>(user);
    void* bound = nullptr;
    Call([&](auto* e) { return duckdb_v2_table_function_init_global_get_bind_data(info, &bound, e); });
    auto scan = OpenScan(spec.rows(*spec.runtime, static_cast<TableBinding*>(bound)->session), context);
    duckdb_v2_opaque state{scan.release(), [](void* p) { delete static_cast<TableScan*>(p); }, nullptr};
    Call([&](auto* e) { return duckdb_v2_table_function_init_global_set_global_state(info, &state, e); });
  });
}

void TableExec(duckdb_v2_table_function_exec_info_handle info, duckdb_v2_context_handle,
               duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* data = nullptr;
    Call([&](auto* e) { return duckdb_v2_table_function_exec_get_global_state(info, &data, e); });
    auto& scan = *static_cast<TableScan*>(data);
    duckdb_v2_data_chunk_handle output = nullptr;
    Call([&](auto* e) { return duckdb_v2_table_function_exec_get_output_chunk(info, &output, e); });
    for (;;) {
      warpduckdb::DataChunk chunk;
      Call([&](auto* e) { return duckdb_v2_arrow_importer_next_chunk(scan.importer.get(), chunk.out(), e); });
      duckdb_v2_vector_handle first = nullptr;
      Call([&](auto* e) { return duckdb_v2_data_chunk_get_vector(output, 0, &first, e); });
      if (chunk) {
        idx_t vectors = 0;
        idx_t rows = 0;
        Call([&](auto* e) { return duckdb_v2_data_chunk_get_vector_count(chunk.get(), &vectors, e); });
        Call([&](auto* e) { return duckdb_v2_data_chunk_get_size(chunk.get(), &rows, e); });
        for (idx_t i = 0; i < vectors; i++) {
          duckdb_v2_vector_handle source = nullptr;
          duckdb_v2_vector_handle target = nullptr;
          Call([&](auto* e) { return duckdb_v2_data_chunk_get_vector(chunk.get(), i, &source, e); });
          Call([&](auto* e) { return duckdb_v2_data_chunk_get_vector(output, i, &target, e); });
          Call([&](auto* e) { return duckdb_v2_vector_reference(target, source, e); });
        }
        Call([&](auto* e) { return duckdb_v2_vector_set_size(first, rows, e); });
        return;
      }
      if (scan.input_finished) {
        Call([&](auto* e) { return duckdb_v2_vector_set_size(first, 0, e); });
        return;
      }
      ArrowArray array{};
      if (int code = scan.stream.get_next(&scan.stream, &array); code != 0) {
        throw Error(ErrorCode::kExecutionError, "cannot read the table's rows");
      }
      if (array.release == nullptr) {
        scan.input_finished = true;
        Call([&](auto* e) { return duckdb_v2_arrow_importer_append(scan.importer.get(), nullptr, false, true, e); });
      } else {
        Call([&](auto* e) { return duckdb_v2_arrow_importer_append(scan.importer.get(), &array, true, false, e); });
        if (array.release != nullptr) array.release(&array);
      }
    }
  });
}

void RegisterTable(duckdb_v2_connection_handle connection, duckdb_v2_factory_handle factory, Runtime& runtime,
                   const std::string& name, TableProvider rows) {
  TableFunction function;
  Call([&](auto* e) { return duckdb_v2_table_function_create(factory, function.out(), e); });
  const duckdb_v2_str function_name = Str(name);
  Call([&](auto* e) { return duckdb_v2_table_function_set_name(function.get(), &function_name, e); });
  auto spec = std::make_unique<TableSpec>(TableSpec{&runtime, std::move(rows)});
  duckdb_v2_opaque user{spec.release(), [](void* p) { delete static_cast<TableSpec*>(p); }, nullptr};
  Call([&](auto* e) { return duckdb_v2_table_function_set_user_data(function.get(), &user, e); });
  Call([&](auto* e) { return duckdb_v2_table_function_set_bind_callback(function.get(), TableBind, e); });
  Call([&](auto* e) { return duckdb_v2_table_function_set_init_global_callback(function.get(), TableInit, e); });
  Call([&](auto* e) { return duckdb_v2_table_function_set_exec_callback(function.get(), TableExec, e); });
  Call([&](auto* e) { return duckdb_v2_connection_register_table_function(connection, function.get(), e); });
}

const Session& RequireSession(const Session* session, const std::string& function) {
  if (session == nullptr) {
    throw Error(ErrorCode::kSqlError, function + "() is only available in a client session");
  }
  return *session;
}

std::shared_ptr<arrow::RecordBatch> SessionsTable(Runtime& runtime, const Session& session) {
  arrow::StringBuilder id, user, role, method;
  arrow::TimestampBuilder started(arrow::timestamp(arrow::TimeUnit::MICRO, "UTC"), arrow::default_memory_pool());
  arrow::BooleanBuilder running, current;
  // Unrestricted admins see every session (to KILL one); everyone else, a restricted
  // admin included, its own principal's (SeesSession).
  for (const auto& s : runtime.sessions.List(session)) {
    (void)id.Append(s.session_id);
    (void)user.Append(s.username);
    (void)role.Append(s.role);
    (void)method.Append(s.auth_method);
    (void)started.Append(s.started_at_us);
    (void)running.Append(s.running);
    (void)current.Append(s.session_id == session.id());
  }
  std::vector<std::shared_ptr<arrow::Array>> columns(7);
  (void)id.Finish(&columns[0]);
  (void)user.Finish(&columns[1]);
  (void)role.Finish(&columns[2]);
  (void)method.Finish(&columns[3]);
  (void)started.Finish(&columns[4]);
  (void)running.Finish(&columns[5]);
  (void)current.Finish(&columns[6]);
  auto schema = arrow::schema({arrow::field("session_id", arrow::utf8()), arrow::field("username", arrow::utf8()),
                               arrow::field("role", arrow::utf8()), arrow::field("auth_method", arrow::utf8()),
                               arrow::field("start_time", arrow::timestamp(arrow::TimeUnit::MICRO, "UTC")),
                               arrow::field("running", arrow::boolean()), arrow::field("is_current", arrow::boolean())});
  return arrow::RecordBatch::Make(schema, columns[0]->length(), columns);
}

std::shared_ptr<arrow::RecordBatch> MetricsTable(Runtime& runtime) {
  if (!runtime.metrics.enabled()) {
    throw Error(ErrorCode::kSqlError, "hardcharger_metrics() is disabled: start the server with enable_metrics");
  }
  auto map_type = arrow::map(arrow::utf8(), arrow::utf8());
  arrow::StringBuilder name;
  arrow::DoubleBuilder value;
  auto keys = std::make_shared<arrow::StringBuilder>();
  auto items = std::make_shared<arrow::StringBuilder>();
  arrow::MapBuilder labels(arrow::default_memory_pool(), keys, items, map_type);
  for (const auto& sample : runtime.metrics.Snapshot()) {
    (void)name.Append(sample.name);
    (void)value.Append(sample.value);
    (void)labels.Append();
    for (const auto& [k, v] : sample.labels) {
      (void)keys->Append(k);
      (void)items->Append(v);
    }
  }
  std::vector<std::shared_ptr<arrow::Array>> columns(3);
  (void)name.Finish(&columns[0]);
  (void)value.Finish(&columns[1]);
  (void)labels.Finish(&columns[2]);
  auto schema = arrow::schema({arrow::field("name", arrow::utf8()), arrow::field("value", arrow::float64()),
                               arrow::field("labels", map_type)});
  return arrow::RecordBatch::Make(schema, columns[0]->length(), columns);
}


// ---- hardcharger_active_connections() ----

using Micros = int64_t;  // since the epoch

Micros ToMicros(std::chrono::system_clock::time_point time) {
  return std::chrono::duration_cast<std::chrono::microseconds>(time.time_since_epoch()).count();
}

Micros NowMicros() { return ToMicros(std::chrono::system_clock::now()); }

std::shared_ptr<arrow::DataType> Timestamptz() { return arrow::timestamp(arrow::TimeUnit::MICRO, "UTC"); }

// One row, gathered under the session table's lock and built into arrays outside it.
struct ConnectionRow {
  std::string session_id, username, role, tenant, auth_method;
  std::vector<std::pair<std::string, std::string>> tags;
  std::string transaction;
  bool busy = false;  // running a statement or an ingest (Hardcharger's side)
  Session::Activity activity;
  Micros session_started_at = 0;
  bool killed = false;
  bool is_current = false;
};

// A session's tags: the Warp SQL session_tag option's keys, then SET hardcharger.session_tag's
// (which win). Text values as they are, any other value as JSON.
std::vector<std::pair<std::string, std::string>> SessionTags(const std::string& option, const std::string& setting) {
  std::map<std::string, std::string> tags;
  for (const std::string* source : {&option, &setting}) {
    if (source->empty()) continue;
    boost::system::error_code ec;
    const boost::json::value parsed = boost::json::parse(*source, ec);
    if (ec) continue;
    if (!parsed.is_object()) {
      tags["session_tag"] = boost::json::serialize(parsed);
      continue;
    }
    for (const auto& item : parsed.as_object()) {
      tags[std::string(item.key())] =
          item.value().is_string() ? std::string(item.value().as_string()) : boost::json::serialize(item.value());
    }
  }
  return {tags.begin(), tags.end()};
}

// idle, queued, running, streaming, idle_in_transaction or idle_in_transaction_aborted.
std::string ConnectionState(const ConnectionRow& row, const warpsql::SessionActivity* sdk) {
  if (row.activity.queued_since_us != 0) return "queued";
  if (sdk != nullptr && !sdk->statement_id.empty()) {
    if (sdk->statement_state == "queued") return "queued";
    // Running until its first batch is out (an aggregate computes everything first), then
    // streaming: sending rows, as the engine produces them.
    if (sdk->statement_state == "running") return sdk->statement_batches > 0 ? "streaming" : "running";
    if (sdk->open_result_streams > 0) return "streaming";  // a finished result still being read
  }
  if (row.busy) return "running";
  if (row.transaction == "open") return "idle_in_transaction";
  if (row.transaction == "aborted") return "idle_in_transaction_aborted";
  return "idle";
}

std::shared_ptr<arrow::RecordBatch> ActiveConnectionsTable(Runtime& runtime, const Session& viewer) {
  // The transport's side first (connections, statements, results), then the engine's.
  std::map<std::string, warpsql::SessionActivity> transport;
  if (runtime.server != nullptr) {
    for (auto& activity : runtime.server->Sessions()) transport.emplace(activity.session_id, std::move(activity));
  }
  std::vector<ConnectionRow> rows;
  runtime.sessions.ForEach([&](Session& s) {
    if (!SeesSession(viewer, s)) return;
    ConnectionRow row;
    row.session_id = s.id();
    row.username = s.principal().name;
    row.role = s.principal().role.empty() ? "user" : s.principal().role;
    row.tenant = s.tenant();
    row.auth_method = s.principal().auth_method.empty() ? "token" : s.principal().auth_method;
    row.transaction = std::string(warpsql::TransactionStateName(s.transaction()));
    row.activity = s.activity();
    row.busy = s.running() || row.activity.ingesting;
    row.tags = SessionTags(row.activity.session_tag_option, s.settings().session_tag);
    row.session_started_at = s.started_at_us();
    row.killed = s.killed();
    row.is_current = &s == &viewer;
    rows.push_back(std::move(row));
  });
  std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) {
    return a.session_started_at != b.session_started_at ? a.session_started_at < b.session_started_at
                                                        : a.session_id < b.session_id;
  });

  arrow::MemoryPool* pool = arrow::default_memory_pool();
  arrow::StringBuilder session_id, instance_id, cluster_id, username, role, tenant, auth_method, client_address,
      client_info, tls_version, certificate_identity, certificate_thumbprint, state, transaction, statement_id,
      statement, cancel_reason;
  auto tag_keys = std::make_shared<arrow::StringBuilder>();
  auto tag_values = std::make_shared<arrow::StringBuilder>();
  auto map_type = arrow::map(arrow::utf8(), arrow::utf8());
  arrow::MapBuilder session_tags(pool, tag_keys, tag_values, map_type);
  arrow::BooleanBuilder tls, token_certificate_bound, is_current;
  arrow::TimestampBuilder statement_started_at(Timestamptz(), pool), queued_since(Timestamptz(), pool),
      session_started_at(Timestamptz(), pool), last_activity_at(Timestamptz(), pool);
  arrow::Int32Builder result_partitions, open_result_streams, open_ingest_parts, retained_results;
  arrow::Int64Builder retained_rows;
  auto text = [](arrow::StringBuilder& b, const std::string& v) { (void)(v.empty() ? b.AppendNull() : b.Append(v)); };
  auto when = [](arrow::TimestampBuilder& b, Micros v) { (void)(v == 0 ? b.AppendNull() : b.Append(v)); };
  const std::string& cluster = runtime.config.cluster_id;
  for (const auto& row : rows) {
    const auto found = transport.find(row.session_id);
    const warpsql::SessionActivity* sdk = found == transport.end() ? nullptr : &found->second;
    (void)session_id.Append(row.session_id);
    (void)instance_id.Append(runtime.instance_id);
    text(cluster_id, cluster);
    (void)username.Append(row.username);
    (void)role.Append(row.role);
    text(tenant, row.tenant);
    (void)auth_method.Append(row.auth_method);
    text(client_address, sdk ? sdk->client_address : "");
    text(client_info, sdk ? sdk->user_agent : "");
    (void)session_tags.Append();
    for (const auto& [k, v] : row.tags) {
      (void)tag_keys->Append(k);
      (void)tag_values->Append(v);
    }
    (void)(sdk ? tls.Append(sdk->tls) : tls.AppendNull());
    text(tls_version, sdk ? sdk->tls_version : "");
    text(certificate_identity, sdk ? sdk->certificate_identity : "");
    text(certificate_thumbprint, sdk ? sdk->client_certificate_thumbprint : "");
    (void)(sdk ? token_certificate_bound.Append(sdk->token_certificate_bound) : token_certificate_bound.AppendNull());
    (void)state.Append(ConnectionState(row, sdk));
    (void)transaction.Append(row.transaction);
    text(statement_id, sdk ? sdk->statement_id : "");
    text(statement, row.activity.statement);
    Micros started = row.activity.statement_started_us;
    if (started == 0 && row.activity.queued_since_us == 0 && sdk && sdk->statement_started_at &&
        sdk->statement_state == "running") {
      started = ToMicros(*sdk->statement_started_at);
    }
    when(statement_started_at, started);
    Micros queued = row.activity.queued_since_us;
    if (queued == 0 && sdk && sdk->statement_state == "queued") queued = ToMicros(sdk->statement_submitted_at);
    when(queued_since, queued);
    when(session_started_at, row.session_started_at);
    when(last_activity_at, sdk ? ToMicros(sdk->last_request_at) : 0);
    // While a cancelled statement unwinds: why. A killed session says so until it is gone.
    std::string reason = sdk ? sdk->cancel_reason : "";
    if (reason.empty() && row.killed) reason = "killed";
    if (reason.empty() && row.busy) reason = row.activity.cancel_reason;
    text(cancel_reason, reason);
    (void)(sdk && sdk->result_partitions > 0 ? result_partitions.Append(sdk->result_partitions)
                                             : result_partitions.AppendNull());
    (void)open_result_streams.Append(sdk ? sdk->open_result_streams : 0);
    (void)open_ingest_parts.Append(sdk ? sdk->open_ingest_parts : 0);
    (void)retained_results.Append(sdk ? sdk->retained_results : 0);
    (void)retained_rows.Append(sdk ? sdk->retained_rows : 0);
    (void)is_current.Append(row.is_current);
  }
  std::vector<std::pair<std::string, arrow::ArrayBuilder*>> columns = {
      {"session_id", &session_id},
      {"instance_id", &instance_id},
      {"cluster_id", &cluster_id},
      {"username", &username},
      {"role", &role},
      {"tenant", &tenant},
      {"auth_method", &auth_method},
      {"client_address", &client_address},
      {"client_info", &client_info},
      {"session_tags", &session_tags},
      {"tls", &tls},
      {"tls_version", &tls_version},
      {"certificate_identity", &certificate_identity},
      {"certificate_thumbprint", &certificate_thumbprint},
      {"token_certificate_bound", &token_certificate_bound},
      {"state", &state},
      {"transaction", &transaction},
      {"statement_id", &statement_id},
      {"statement", &statement},
      {"statement_started_at", &statement_started_at},
      {"queued_since", &queued_since},
      {"session_started_at", &session_started_at},
      {"last_activity_at", &last_activity_at},
      {"cancel_reason", &cancel_reason},
      {"result_partitions", &result_partitions},
      {"open_result_streams", &open_result_streams},
      {"open_ingest_parts", &open_ingest_parts},
      {"retained_results", &retained_results},
      {"retained_rows", &retained_rows},
      {"is_current", &is_current},
  };
  std::vector<std::shared_ptr<arrow::Field>> fields;
  std::vector<std::shared_ptr<arrow::Array>> arrays;
  for (auto& [name, builder] : columns) {
    std::shared_ptr<arrow::Array> array;
    (void)builder->Finish(&array);
    fields.push_back(arrow::field(name, array->type()));
    arrays.push_back(std::move(array));
  }
  return arrow::RecordBatch::Make(arrow::schema(fields), static_cast<int64_t>(rows.size()), arrays);
}

// ---- hardcharger_server_info() ----

std::string RbacStoreType(const Config& config) {
  if (config.rbac_store_uri.empty()) return "none";
  return authz::DuckDBStoreFile(config.rbac_store_uri).empty() ? "postgresql" : "duckdb";
}

// One row. Everyone sees what the server is (versions, ids, start time and uptime); the
// host, its listeners, TLS and access control are the unrestricted administrator's, NULL
// for everyone else.
std::shared_ptr<arrow::RecordBatch> ServerInfoTable(Runtime& runtime, const Session& viewer) {
  const InstanceInfo& i = runtime.instance_info;
  const Config& c = runtime.config;
  const bool full = viewer.full_admin();
  const Micros now = NowMicros();
  const Micros started = ToMicros(i.start_time);
  std::vector<std::shared_ptr<arrow::Field>> fields;
  std::vector<std::shared_ptr<arrow::Array>> arrays;
  auto add = [&](const std::string& name, std::shared_ptr<arrow::Array> array) {
    fields.push_back(arrow::field(name, array->type()));
    arrays.push_back(std::move(array));
  };
  auto text = [&](const std::string& name, const std::string& value, bool shown = true) {
    arrow::StringBuilder b;
    (void)(!shown || value.empty() ? b.AppendNull() : b.Append(value));
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add(name, a);
  };
  auto int32 = [&](const std::string& name, std::optional<int32_t> value, bool shown = true) {
    arrow::Int32Builder b;
    (void)(!shown || !value ? b.AppendNull() : b.Append(*value));
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add(name, a);
  };
  auto int64 = [&](const std::string& name, std::optional<int64_t> value, bool shown = true) {
    arrow::Int64Builder b;
    (void)(!shown || !value ? b.AppendNull() : b.Append(*value));
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add(name, a);
  };
  auto boolean = [&](const std::string& name, bool value, bool shown = true) {
    arrow::BooleanBuilder b;
    (void)(!shown ? b.AppendNull() : b.Append(value));
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add(name, a);
  };
  auto port = [](int64_t configured) { return configured > 0 ? std::optional<int32_t>(static_cast<int32_t>(configured)) : std::nullopt; };

  text("hardcharger_version", i.hardcharger_version);
  text("duckdb_version", i.duckdb_version);
  text("arrow_version", i.arrow_version);
  text("instance_id", i.instance_id);
  text("cluster_id", i.cluster_id);
  text("instance_tag", i.instance_tag);
  {
    arrow::TimestampBuilder b(Timestamptz(), arrow::default_memory_pool());
    (void)b.Append(started);
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add("start_time", a);
  }
  {
    arrow::MonthDayNanoIntervalBuilder b;
    (void)b.Append(arrow::MonthDayNanoIntervalType::MonthDayNanos{0, 0, (now - started) * 1000});
    std::shared_ptr<arrow::Array> a;
    (void)b.Finish(&a);
    add("uptime", a);
  }
  int64("uptime_seconds", (now - started) / 1000000);
  // The unrestricted administrator's.
  text("hostname", i.hostname, full);
  text("os_platform", i.os_platform, full);
  text("os_name", i.os_name, full);
  text("os_version", i.os_version, full);
  text("cpu_arch", i.cpu_arch, full);
  text("cpu_model", i.cpu_model, full);
  int32("cpu_count", i.cpu_count, full);
  int64("memory_total_bytes", i.memory_total_bytes, full);
  const double resident = ResidentMemoryBytes();
  int64("memory_resident_bytes",
        resident == resident ? std::optional<int64_t>(static_cast<int64_t>(resident)) : std::nullopt, full);
  text("database_path", i.database_path, full);
  boolean("read_only", i.readonly, full);
  text("listen_address", i.hostname_arg, full);
  int32("port", runtime.server ? std::optional<int32_t>(runtime.server->port()) : port(c.port), full);
  int32("health_port", port(c.health_port), full);
  text("metrics_address", c.enable_metrics && c.metrics_port > 0 ? c.metrics_bind_address : "", full);
  int32("metrics_port", c.enable_metrics ? port(c.metrics_port) : std::nullopt, full);
  boolean("oauth_enabled", !c.oauth_client_id.empty(), full);
  boolean("tls_enabled", i.tls_enabled, full);
  boolean("mtls_enabled", i.mtls_required, full);
  boolean("mtls_client_certificate_required", i.mtls_required && c.mtls_require_client_cert, full);
  text("server_certificate_sha256", i.server_certificate_sha256, full);
  boolean("web_ui_enabled", c.web_ui, full);
  text("rbac_store_type", RbacStoreType(c), full);
  int32("session_count", static_cast<int32_t>(runtime.sessions.size()), full);
  return arrow::RecordBatch::Make(arrow::schema(fields), 1, arrays);
}

// ---- hardcharger_cancel(session_id) ----

struct CancelBinding {
  Runtime* runtime;
  Session* session;  // the caller
};

void CancelBind(duckdb_v2_function_bind_info_handle info, duckdb_v2_scalar_function_bind_info_handle result,
                duckdb_v2_context_handle, duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* user = nullptr;
    Call([&](auto* e) { return duckdb_v2_function_bind_get_user_data(info, &user, e); });
    Session* caller = CurrentSession();
    if (caller == nullptr) {
      throw Error(ErrorCode::kSqlError, "hardcharger_cancel() is only available in a client session");
    }
    duckdb_v2_opaque data{new CancelBinding{static_cast<Runtime*>(user), caller},
                          [](void* p) { delete static_cast<CancelBinding*>(p); }, nullptr};
    Call([&](auto* e) { return duckdb_v2_scalar_function_bind_set_bind_data(result, &data, e); });
  });
}

// Cancels what session `id` runs for `caller`; returns whether there was anything.
bool CancelFor(Runtime& runtime, Session& caller, std::string id) {
  std::transform(id.begin(), id.end(), id.begin(), [](unsigned char ch) { return std::tolower(ch); });
  switch (runtime.sessions.Find(id, caller)) {
    case SessionRegistry::Lookup::kOwnSession:
      throw Error(ErrorCode::kSqlError,
                  "hardcharger_cancel() can't cancel the session that calls it: call it from another session",
                  "55000");
    case SessionRegistry::Lookup::kNotFound:
    case SessionRegistry::Lookup::kHidden:
      if (!caller.full_admin()) {
        // The same answer whether or not the session exists: it isn't the caller's.
        throw Error(ErrorCode::kPermissionDenied,
                    "Session '" + id + "' is not one of your sessions: only an administrator without "
                    "catalog_access rules may cancel another user's session",
                    "42501");
      }
      throw Error(ErrorCode::kSqlError, "Session '" + id + "' not found", "42704");
    case SessionRegistry::Lookup::kVisible:
      break;
  }
  const auto stopped = CancelSessionWork(runtime, id, warpsql::SessionCancelReason::kCancelled);
  if (!stopped) throw Error(ErrorCode::kSqlError, "Session '" + id + "' not found", "42704");
  Log(LogLevel::kInfo, "hardcharger_cancel()",
      {{"target", id}, {"user", caller.principal().name}, {"session_id", caller.id()},
       {"cancelled", std::to_string(*stopped)}});
  return *stopped > 0;
}

void CancelExec(duckdb_v2_scalar_function_exec_info_handle info, duckdb_v2_context_handle context,
                duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* data = nullptr;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_bind_data(info, &data, e); });
    const auto& binding = *static_cast<const CancelBinding*>(data);
    duckdb_v2_factory_handle factory = nullptr;
    Call([&](auto* e) { return duckdb_v2_context_get_factory(context, &factory, e); });
    idx_t rows = 0;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_row_count(info, &rows, e); });
    duckdb_v2_vector_handle input = nullptr;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_arg(info, 0, &input, e); });
    duckdb_v2_vector_handle output = nullptr;
    Call([&](auto* e) { return duckdb_v2_scalar_function_exec_get_result(info, &output, e); });
    auto boolean = Type(factory, "BOOLEAN");
    Call([&](auto* e) { return duckdb_v2_vector_set_size(output, rows, e); });  // written row by row
    for (idx_t row = 0; row < rows; row++) {
      warpduckdb::Value in;
      Call([&](auto* e) { return duckdb_v2_vector_get_value(input, row, in.out(), e); });
      bool null = true;
      Call([&](auto* e) { return duckdb_v2_value_is_null(in.get(), &null, e); });
      warpduckdb::Value out;
      if (null) {
        Call([&](auto* e) { return duckdb_v2_value_create_null(factory, boolean.get(), out.out(), e); });
      } else {
        duckdb_v2_str id{};
        Call([&](auto* e) { return duckdb_v2_value_get_varchar(in.get(), &id, e); });
        const bool cancelled = CancelFor(*binding.runtime, *binding.session, std::string(id.ptr, id.len));
        Call([&](auto* e) { return duckdb_v2_value_create_bool(factory, cancelled, out.out(), e); });
      }
      Call([&](auto* e) { return duckdb_v2_vector_set_value(output, row, out.get(), e); });
    }
  });
}

void RegisterCancel(duckdb_v2_connection_handle connection, duckdb_v2_factory_handle factory, Runtime& runtime) {
  ScalarFunction function;
  Call([&](auto* e) { return duckdb_v2_scalar_function_create(factory, function.out(), e); });
  const duckdb_v2_str function_name = Str("hardcharger_cancel");
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_name(function.get(), &function_name, e); });
  duckdb_v2_function_signature_handle signature = nullptr;
  Call([&](auto* e) { return duckdb_v2_scalar_function_get_signature(function.get(), &signature, e); });
  auto varchar = Type(factory, "VARCHAR");
  const duckdb_v2_identifier_t parameter = Str("session_id");
  Call([&](auto* e) {
    return duckdb_v2_function_signature_add_parameter(signature, &parameter, varchar.get(), nullptr,
                                                      DUCKDB_V2_FUNCTION_PARAMETER_KIND_POSITIONAL_ONLY, e);
  });
  auto boolean = Type(factory, "BOOLEAN");
  Call([&](auto* e) { return duckdb_v2_function_signature_set_return_type(signature, boolean.get(), e); });
  // It acts: never folded at planning, never shared between rows or statements.
  Call([&](auto* e) {
    return duckdb_v2_scalar_function_set_property(function.get(), DUCKDB_V2_FUNCTION_PROPERTY_STABILITY,
                                                  DUCKDB_V2_FUNCTION_PROPERTY_STABILITY_VOLATILE, e);
  });
  duckdb_v2_opaque user{&runtime, nullptr, nullptr};
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_user_data(function.get(), &user, e); });
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_bind_callback(function.get(), CancelBind, e); });
  Call([&](auto* e) { return duckdb_v2_scalar_function_set_exec_callback(function.get(), CancelExec, e); });
  Call([&](auto* e) { return duckdb_v2_connection_register_scalar_function(connection, function.get(), e); });
}

}  // namespace

void RegisterSqlFunctions(duckdb_v2_connection_handle connection, Runtime& runtime) {
  duckdb_v2_factory_handle factory = nullptr;
  Call([&](auto* e) { return duckdb_v2_connection_get_factory(connection, &factory, e); });

  using Fn = std::function<ContextValue(Runtime&, const Session*)>;
  auto text = [](auto get) -> Fn {
    return [get](Runtime& r, const Session* s) { return ContextValue{get(r, s), std::nullopt}; };
  };
  const std::vector<std::tuple<std::string, std::string_view, Fn>> scalars = {
      {"USER", "VARCHAR", text([](Runtime&, const Session* s) -> std::optional<std::string> {
         return s ? std::optional(s->principal().name) : std::nullopt;
       })},
      {"ROLE", "VARCHAR", text([](Runtime&, const Session* s) -> std::optional<std::string> {
         return s ? std::optional(s->principal().role) : std::nullopt;
       })},
      {"VERSION", "VARCHAR",
       text([](Runtime&, const Session*) -> std::optional<std::string> { return std::string(kVersion); })},
      {"CURRENT_SESSION", "VARCHAR", text([](Runtime&, const Session* s) -> std::optional<std::string> {
         return s ? std::optional(s->id()) : std::nullopt;
       })},
      {"CURRENT_INSTANCE", "VARCHAR",
       text([](Runtime& r, const Session*) -> std::optional<std::string> { return r.instance_id; })},
      {"CURRENT_CLUSTER", "VARCHAR", text([](Runtime& r, const Session*) -> std::optional<std::string> {
         return r.config.cluster_id.empty() ? std::nullopt : std::optional(r.config.cluster_id);
       })},
      {"INSTRUMENTATION_ENABLED", "BOOLEAN",
       [](Runtime& r, const Session*) { return ContextValue{std::nullopt, r.instrumentation != nullptr}; }},
      {"INSTRUMENTATION_CATALOG", "VARCHAR", text([](Runtime& r, const Session*) -> std::optional<std::string> {
         return r.instrumentation ? std::optional(r.instrumentation->catalog()) : std::nullopt;
       })},
      {"INSTRUMENTATION_SCHEMA", "VARCHAR", text([](Runtime& r, const Session*) -> std::optional<std::string> {
         return r.instrumentation ? std::optional(r.instrumentation->schema()) : std::nullopt;
       })},
  };
  for (const auto& [suffix, type, value] : scalars) {
    RegisterScalar(connection, factory, runtime, "HARDCHARGER_" + std::string(suffix), type, value);
  }

  RegisterTable(connection, factory, runtime, "hardcharger_settings", [](Runtime&, const Session* s) {
    return SettingsTable(RequireSession(s, "hardcharger_settings"));
  });
  RegisterTable(connection, factory, runtime, "hardcharger_sessions", [](Runtime& r, const Session* s) {
    return SessionsTable(r, RequireSession(s, "hardcharger_sessions"));
  });
  RegisterTable(connection, factory, runtime, "hardcharger_metrics",
                [](Runtime& r, const Session*) { return MetricsTable(r); });
  RegisterTable(connection, factory, runtime, "hardcharger_active_connections", [](Runtime& r, const Session* s) {
    return ActiveConnectionsTable(r, RequireSession(s, "hardcharger_active_connections"));
  });
  RegisterTable(connection, factory, runtime, "hardcharger_server_info", [](Runtime& r, const Session* s) {
    return ServerInfoTable(r, RequireSession(s, "hardcharger_server_info"));
  });
  RegisterCancel(connection, factory, runtime);
}

}  // namespace hardcharger
