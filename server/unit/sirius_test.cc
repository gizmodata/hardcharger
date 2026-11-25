// NVIDIA's Sirius extension on a Hardcharger server (authz/sirius.hpp), without Sirius: an
// extension load that registers what Sirius registers (a read-only file system for every
// s3:// path named SiriusHttpFS, and settings), through DuckDB's own load bookkeeping, so the
// server's extension callbacks run as they do for `LOAD sirius`. The real extension pair is
// exercised by server/tests/test_sirius_live.py (marker `sirius`) and on a GPU node.
#include <gtest/gtest.h>

#include <duckdb.hpp>
#include <duckdb/common/virtual_file_system.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/extension_install_info.hpp>
#include <duckdb/main/extension_manager.hpp>

#include <algorithm>
#include <string>

#include "authz/sirius.hpp"

namespace dd = duckdb;
using hardcharger::authz::InstallSiriusPolicy;
using hardcharger::authz::IsSiriusAdministratorOnlyFunction;
using hardcharger::authz::IsSiriusSetting;
using hardcharger::authz::IsSiriusStartupOnlySetting;
using hardcharger::authz::kSiriusS3FileSystem;
using hardcharger::authz::SiriusReadParquetRefusal;

namespace {

// What Sirius registers for s3:// (sirius_httpfs.hpp): every s3://bucket/key, manually set (so
// DuckDB picks it whatever else handles s3://), refusing every write.
class FakeSiriusHttpFs : public dd::FileSystem {
 public:
  bool CanHandleFile(const dd::string& path) override { return path.rfind("s3://", 0) == 0; }
  std::string GetName() const override { return kSiriusS3FileSystem; }
  bool IsManuallySet() override { return true; }
  dd::unique_ptr<dd::FileHandle> OpenFile(const dd::string& path, dd::FileOpenFlags,
                                          dd::optional_ptr<dd::FileOpener>) override {
    throw dd::IOException("[sirius_httpfs] '" + path + "' is read-only; S3 writes (COPY TO) are not supported");
  }
};

// A load named `extension` that registers what Sirius does, through DuckDB's ExtensionManager
// (so OnBeginExtensionLoad / OnExtensionLoaded fire as for LOAD).
void Load(dd::DatabaseInstance& db, const std::string& extension, const std::string& setting, bool file_system) {
  auto load = dd::ExtensionManager::Get(db).BeginLoad(dd::ExtensionLoadOptions(extension));
  ASSERT_TRUE(load);
  auto& config = dd::DBConfig::GetConfig(db);
  config.AddExtensionOption(dd::Identifier(setting), "test", dd::LogicalType::VARCHAR, dd::Value(""));
  if (file_system) config.file_system->RegisterSubSystem(dd::make_uniq<FakeSiriusHttpFs>());
  dd::ExtensionInstallInfo info;
  load->FinishLoad(info);
}

bool HasSubSystem(dd::DatabaseInstance& db, const std::string& name) {
  const auto names = dd::DBConfig::GetConfig(db).file_system->ListSubSystems();
  return std::find(names.begin(), names.end(), name) != names.end();
}

}  // namespace

// The fix for writes and CPU reads of S3 with Sirius loaded: the load that registers Sirius's
// s3:// file system has it taken out again, so s3:// reaches whatever else serves it (httpfs).
TEST(Sirius, ItsS3FileSystemLeavesDuckDBsWhenItLoads) {
  dd::DuckDB db(nullptr);
  InstallSiriusPolicy(*db.instance);
  dd::Connection connection(db);
  ASSERT_FALSE(connection.Query("SET autoinstall_known_extensions = false")->HasError());
  ASSERT_FALSE(connection.Query("SET autoload_known_extensions = false")->HasError());

  Load(*db.instance, "sirius", "test_sirius_knob_a", /*file_system=*/true);
  EXPECT_FALSE(HasSubSystem(*db.instance, kSiriusS3FileSystem));
  auto result = connection.Query("COPY (SELECT 1 AS x) TO 's3://bucket/out/x.csv'");
  ASSERT_TRUE(result->HasError());
  EXPECT_EQ(result->GetError().find("sirius_httpfs"), std::string::npos) << result->GetError();
  EXPECT_NE(result->GetError().find("httpfs"), std::string::npos) << result->GetError();  // DuckDB asks for httpfs
}

// The positive control: without the policy the fake does what Sirius's does.
TEST(Sirius, WithoutThePolicyItsS3FileSystemRefusesWrites) {
  dd::DuckDB db(nullptr);
  dd::Connection connection(db);
  ASSERT_FALSE(connection.Query("SET autoinstall_known_extensions = false")->HasError());
  ASSERT_FALSE(connection.Query("SET autoload_known_extensions = false")->HasError());
  Load(*db.instance, "sirius_control", "test_sirius_knob_control", /*file_system=*/true);
  EXPECT_TRUE(HasSubSystem(*db.instance, kSiriusS3FileSystem));
  auto result = connection.Query("COPY (SELECT 1 AS x) TO 's3://bucket/out/x.csv'");
  ASSERT_TRUE(result->HasError());
  EXPECT_NE(result->GetError().find("[sirius_httpfs]"), std::string::npos) << result->GetError();
}

// Settings an extension named sirius* registers while it loads (and those its nested backend
// load registers) are Sirius's, whatever their names; another extension's are not.
TEST(Sirius, TheSettingsASiriusLoadRegistersAreSirius) {
  dd::DuckDB db(nullptr);
  InstallSiriusPolicy(*db.instance);
  EXPECT_FALSE(IsSiriusSetting("brand_new_gpu_knob"));
  Load(*db.instance, "sirius", "brand_new_gpu_knob", /*file_system=*/false);
  EXPECT_TRUE(IsSiriusSetting("brand_new_gpu_knob"));
  EXPECT_TRUE(IsSiriusSetting("BRAND_NEW_GPU_KNOB"));
  Load(*db.instance, "not_sirius_ext", "someone_elses_knob", /*file_system=*/false);
  EXPECT_FALSE(IsSiriusSetting("someone_elses_knob"));
  // A nested load (the loader loading its CUDA backend): the backend's settings are Sirius's too.
  {
    auto outer = dd::ExtensionManager::Get(*db.instance).BeginLoad(dd::ExtensionLoadOptions("sirius_loader2"));
    ASSERT_TRUE(outer);
    Load(*db.instance, "sirius_cuda99", "backend_only_knob", /*file_system=*/false);
    dd::DBConfig::GetConfig(*db.instance)
        .AddExtensionOption(dd::Identifier("loader_only_knob"), "test", dd::LogicalType::VARCHAR, dd::Value(""));
    dd::ExtensionInstallInfo info;
    outer->FinishLoad(info);
  }
  EXPECT_TRUE(IsSiriusSetting("backend_only_knob"));
  EXPECT_TRUE(IsSiriusSetting("loader_only_knob"));
}

TEST(Sirius, KnownSettingsAndTheOnesOnlyTheStartupSets) {
  // The build this was written against, before any load.
  for (const char* name : {"gpu_execution", "enable_duckdb_fallback", "spill_compression", "sirius_log_level",
                           "pin_table_input_compression_plan_dir", "sirius_exchange_peers"}) {
    EXPECT_TRUE(IsSiriusSetting(name)) << name;
  }
  for (const char* name : {"memory_limit", "threads", "s3_region", "TimeZone", "search_path"}) {
    EXPECT_FALSE(IsSiriusSetting(name)) << name;
  }
  for (const char* name :
       {"sirius_log_backend", "sirius_log_dir", "sirius_log_level", "sirius_exchange_peers", "sirius_exchange_rank",
        "sirius_exchange_host_staging", "pin_table_input_compression_plan_dir", "sirius_some_future_dir"}) {
    EXPECT_TRUE(IsSiriusStartupOnlySetting(name)) << name;
  }
  for (const char* name :
       {"gpu_execution", "enable_duckdb_fallback", "sirius_log_flush_seconds", "memory_limit", "temp_directory"}) {
    EXPECT_FALSE(IsSiriusStartupOnlySetting(name)) << name;
  }
}

TEST(Sirius, AdministratorOnlyFunctions) {
  for (const char* name : {"gpu_execution", "sirius_stream_source", "pin_table", "unpin_table", "sirius_knn_search",
                           "sirius_create_ann_index", "sirius_drop_ann_index", "reset_sirius_cache", "profiler_start",
                           "profiler_stop", "sirius_read_parquet"}) {
    EXPECT_TRUE(IsSiriusAdministratorOnlyFunction(name)) << name;
  }
  for (const char* name : {"sirius_status", "read_parquet", "query", "range"}) {
    EXPECT_FALSE(IsSiriusAdministratorOnlyFunction(name)) << name;
  }
}

// sirius_read_parquet() reads with Sirius's own I/O: only the s3:// form Sirius's rewrite makes.
TEST(Sirius, SiriusReadParquetTakesOneS3LiteralOnly) {
  for (const char* sql : {"SELECT 1", "SELECT * FROM read_parquet('/data/x.parquet')",
                          "SELECT * FROM sirius_read_parquet('s3://bucket/k.parquet')",
                          "SELECT * FROM SIRIUS_READ_PARQUET('S3://bucket/k.parquet')",
                          "SELECT * FROM main.sirius_read_parquet('s3://b/k.parquet', hive_partitioning = true)",
                          "SELECT 'sirius_read_parquet(''/etc/passwd'')' AS just_text",
                          "-- sirius_read_parquet('/etc/passwd')\nSELECT 1", "SELECT 1 AS sirius_read_parquet"}) {
    EXPECT_FALSE(SiriusReadParquetRefusal(sql).has_value()) << sql;
  }
  for (const char* sql :
       {"SELECT * FROM sirius_read_parquet('/opt/gizmosql/catalogs/x.parquet')",
        "SELECT * FROM sirius_read_parquet('file:///etc/passwd')",
        "SELECT * FROM \"sirius_read_parquet\"('/etc/passwd')",
        "SELECT * FROM sirius_read_parquet /* c */ ( '/etc/passwd' )",
        "SELECT * FROM sirius_read_parquet('s3:' || '//b/k')", "SELECT * FROM sirius_read_parquet(getvariable('p'))",
        "SELECT * FROM sirius_read_parquet(E's3://b/k')", "SELECT * FROM sirius_read_parquet($$s3://b/k$$)",
        "SELECT * FROM sirius_read_parquet(['s3://b/k', '/etc/passwd'])", "SELECT * FROM sirius_read_parquet()"}) {
    EXPECT_TRUE(SiriusReadParquetRefusal(sql).has_value()) << sql;
  }
}
