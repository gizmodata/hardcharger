// Small helpers over DuckDB's C++ API for the access-control module, the one part of
// Hardcharger that uses DuckDB's internal headers. Everything else talks to DuckDB
// through its v2 C API (duckdb_v2.h).
#pragma once

#include <string>
#include <unordered_set>

#include <duckdb.hpp>
#include <duckdb/parser/expression/constant_expression.hpp>
#include <duckdb/parser/parser.hpp>

namespace hardcharger::authz::ddb {

inline const std::string& Str(const std::string& name) { return name; }
inline const std::string& Str(const duckdb::Identifier& name) { return name.GetIdentifierName(); }

// Plain-string copy of a list of names.
template <class Name>
duckdb::vector<std::string> Strs(const duckdb::vector<Name>& names) {
  duckdb::vector<std::string> result;
  result.reserve(names.size());
  for (const auto& name : names) result.push_back(Str(name));
  return result;
}

// A parser with DuckDB's built-in grammar and no client context.
inline duckdb::Parser StandaloneParser() { return duckdb::Parser::GetBuiltinParser(); }

// Value at (column, row) of a materialized result.
inline duckdb::Value ResultValue(duckdb::QueryResult& result, duckdb::idx_t column,
                                 duckdb::idx_t row) {
  return result.Collection().GetValue(column, row);
}

inline duckdb::idx_t ResultRowCount(duckdb::QueryResult& result) { return result.RowCount(); }

// Every DuckDB 2.0 QueryResult materializes on demand (Collection(), RowCount()).
inline duckdb::unique_ptr<duckdb::QueryResult> ExecuteMaterialized(
    duckdb::PreparedStatement& statement, duckdb::vector<duckdb::Value>& values) {
  return statement.Execute(values);
}

inline duckdb::Value ConstantValue(const duckdb::ConstantExpression& constant) {
  return constant.GetLiteral().ToValue();
}

// Table functions that read files or URLs (DuckDB's own and its bundled extensions').
// Under access control they need the READ FILESYSTEM system privilege.
inline bool IsFileReadingTableFunction(const std::string& name_lower) {
  static const std::unordered_set<std::string> kNames = {
      "read_csv",          "read_csv_auto",         "read_parquet",        "parquet_scan",
      "read_json",         "read_json_auto",        "read_json_objects",   "read_ndjson",
      "read_ndjson_auto",  "read_ndjson_objects",   "read_text",           "read_blob",
      "glob",              "sniff_csv",             "parquet_metadata",    "parquet_schema",
      "parquet_file_metadata", "parquet_kv_metadata", "read_text_auto",
      "read_single_csv_file", "read_single_json_file", "read_duckdb", "read_xlsx",
      "parquet_bloom_probe", "read_json_objects_auto", "read_ndjson_objects_auto"};
  return kNames.contains(name_lower);
}

}  // namespace hardcharger::authz::ddb
