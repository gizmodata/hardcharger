// Hardcharger's engine: Warp SQL's DuckDB backend, with Hardcharger's authorization and
// operations around it.
#pragma once

#include <memory>
#include <string>

#include "config.hpp"
#include "duckdb_backend.hpp"
#include "duckdb_session.hpp"
#include "license.hpp"

namespace hardcharger {

class Backend : public warpduckdb::DuckDBBackend {
 public:
  // Opens the database and runs the init SQL; throws (and the server doesn't start) if
  // any of it fails.
  Backend(const Config& config, const License& license);

  warpsql::ServerIdentity Identity() override;
  boost::json::object ServerInfo() override;

 protected:
  std::unique_ptr<warpsql::EngineSession> NewSession(const warpsql::Principal& principal) override;

 private:
  void RunInitSql(const Config& config);

  const Config& config_;
  const License& license_;
};

class Session : public warpduckdb::DuckDBSession {
 public:
  Session(duckdb_v2_instance_handle instance, warpsql::Principal principal, bool print_queries);

  warpsql::StatementOutcome Execute(std::string_view sql,
                                    const boost::json::value& parameters) override;
  std::unique_ptr<warpsql::PreparedStatement> Prepare(std::string_view sql) override;
  int64_t Ingest(const warpsql::IngestTarget& target,
                 std::shared_ptr<arrow::RecordBatchReader> rows) override;

 private:
  // Refuses statements the principal's role may not run.
  void Authorize(std::string_view sql);

  warpsql::Principal principal_;
  bool print_queries_;
};

}  // namespace hardcharger
