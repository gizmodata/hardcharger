// Hardcharger's engine: Warp SQL's DuckDB backend, with Hardcharger's authorization and
// operations around it.
#pragma once

#include <memory>
#include <string>

#include "authz/authz.hpp"
#include "config.hpp"
#include "duckdb_backend.hpp"
#include "duckdb_session.hpp"
#include "license.hpp"

namespace hardcharger {

class Backend : public warpduckdb::DuckDBBackend {
 public:
  // Opens the database and runs the init SQL; throws (and the server doesn't start) if
  // any of it fails. With `access`, every session is under access control.
  Backend(const Config& config, const License& license,
          std::shared_ptr<authz::AccessControl> access = nullptr);

  warpsql::ServerIdentity Identity() override;
  boost::json::object ServerInfo() override;

 protected:
  std::unique_ptr<warpsql::EngineSession> NewSession(const warpsql::Principal& principal) override;

 private:
  void RunInitSql(const Config& config);

  const Config& config_;
  const License& license_;
  std::shared_ptr<authz::AccessControl> access_;
};

class Session : public warpduckdb::DuckDBSession {
 public:
  // With `access`, the session's connection is bound to `principal` before anything
  // runs on it; a session that cannot be bound is refused (the constructor throws).
  Session(duckdb_v2_instance_handle instance, warpsql::Principal principal, bool print_queries,
          authz::AccessControl* access);
  ~Session() override;

  warpsql::StatementOutcome Execute(std::string_view sql,
                                    const boost::json::value& parameters) override;
  std::unique_ptr<warpsql::PreparedStatement> Prepare(std::string_view sql) override;
  warpsql::StatementOutcome ExecutePrepared(warpsql::PreparedStatement& statement,
                                            const warpsql::Parameters& parameters) override;
  int64_t Ingest(const warpsql::IngestTarget& target,
                 std::shared_ptr<arrow::RecordBatchReader> rows) override;
  std::shared_ptr<arrow::RecordBatchReader> Metadata(warpsql::MetadataKind kind,
                                                     const warpsql::MetadataFilter& filter) override;
  warpsql::OptionErrors SetOptions(const boost::json::object& options) override;
  boost::json::object Options() override;
  void Commit() override;
  void Rollback() override;

 private:
  // Refuses statements the principal's role may not run.
  void Authorize(std::string_view sql);
  // Under access control, throws unless the session is bound and still live.
  void RequireLive() const;

  warpsql::Principal principal_;
  bool print_queries_;
  bool access_controlled_;
  std::shared_ptr<authz::SessionGuard> guard_;  // set exactly when access_controlled_
};

}  // namespace hardcharger
