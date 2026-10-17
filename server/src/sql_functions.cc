#include "sql_functions.hpp"

#include <arrow/api.h>
#include <arrow/c/bridge.h>

#include <functional>
#include <memory>
#include <optional>
#include <string>

#include "duckdb_v2.hpp"
#include "engine.hpp"
#include "instrumentation.hpp"
#include "runtime.hpp"
#include "settings.hpp"
#include "version.hpp"

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

// ---- Table functions over Arrow batches built at bind time ----

using TableProvider = std::function<std::shared_ptr<arrow::RecordBatch>(Runtime&, const Session*)>;

struct TableSpec {
  Runtime* runtime;
  TableProvider rows;
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

void TableBind(duckdb_v2_function_bind_info_handle info, duckdb_v2_table_function_bind_info_handle result,
               duckdb_v2_context_handle context, duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* user = nullptr;
    Call([&](auto* e) { return duckdb_v2_function_bind_get_user_data(info, &user, e); });
    const auto& spec = *static_cast<TableSpec*>(user);
    auto batch = spec.rows(*spec.runtime, CurrentSession());
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
    duckdb_v2_opaque data{scan.release(), [](void* p) { delete static_cast<TableScan*>(p); }, nullptr};
    Call([&](auto* e) { return duckdb_v2_function_bind_set_bind_data(info, &data, e); });
  });
}

void TableExec(duckdb_v2_table_function_exec_info_handle info, duckdb_v2_context_handle,
               duckdb_v2_error_info_handle* err) {
  Guarded(err, [&] {
    void* data = nullptr;
    Call([&](auto* e) { return duckdb_v2_table_function_exec_get_bind_data(info, &data, e); });
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
  for (const auto& s : runtime.sessions.List()) {
    // Admins see every session (to KILL one); others only their own.
    if (!session.admin() && s.session_id != session.id()) continue;
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
  if (!runtime.license.Has("metrics") || !runtime.metrics.enabled()) {
    throw Error(ErrorCode::kSqlError,
                "gizmosql_metrics() is disabled: start the server with enable_metrics and a license with the "
                "'metrics' feature");
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
       text([](Runtime&, const Session*) -> std::optional<std::string> { return std::string("v") + kVersion; })},
      {"EDITION", "VARCHAR",
       text([](Runtime& r, const Session*) -> std::optional<std::string> { return r.license.Edition(); })},
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
    for (const char* prefix : {"HARDCHARGER_", "GIZMOSQL_"}) {
      RegisterScalar(connection, factory, runtime, std::string(prefix) + suffix, type, value);
    }
  }

  for (const char* prefix : {"hardcharger", "gizmosql"}) {
    const bool gizmosql_names = std::string_view(prefix) == "gizmosql";
    RegisterTable(connection, factory, runtime, std::string(prefix) + "_settings",
                  [gizmosql_names, prefix](Runtime&, const Session* s) {
                    return SettingsTable(RequireSession(s, std::string(prefix) + "_settings"), gizmosql_names);
                  });
    RegisterTable(connection, factory, runtime, std::string(prefix) + "_sessions",
                  [prefix](Runtime& r, const Session* s) {
                    return SessionsTable(r, RequireSession(s, std::string(prefix) + "_sessions"));
                  });
    RegisterTable(connection, factory, runtime, std::string(prefix) + "_metrics",
                  [](Runtime& r, const Session*) { return MetricsTable(r); });
  }
}

}  // namespace hardcharger
