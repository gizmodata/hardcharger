// NVIDIA's Sirius extension (GPU execution for DuckDB, `LOAD sirius`) on a Hardcharger server.
//
// Sirius, as it loads, registers three things the server must answer for:
//
// - A file system for every s3:// path ("SiriusHttpFS"). It is read-only and serves only the
//   GPU's scans (it refuses CPU reads, and every read while the GPU is unusable), and it marks
//   itself manually set, so DuckDB picks it over httpfs whatever the load order: with it loaded,
//   COPY ... TO 's3://...', every DuckLake or Iceberg write to S3 and every CPU read of S3 fail.
//   Sirius's GPU scans never read through it: the GPU plan installs the S3 credentials itself,
//   from the bound file list, and reads with its own client; the file system only serves the
//   footer read DuckDB's bind of read_parquet makes, which httpfs serves as well. So the server
//   takes it out of DuckDB's file system the moment a load registers it (the init SQL's LOAD,
//   a later one, with or without a usable GPU): httpfs then serves every s3:// open.
//
// - Table functions that run SQL text or name tables by string: gpu_execution('<sql>') replays
//   its SQL on a connection of its own, with no session state and so with none of the
//   server's checks (no grant, row policy, mask or read-only session applies there);
//   pin_table, unpin_table, sirius_knn_search and the ANN-index functions read or change a
//   table they are handed by name; sirius_stream_source reads a fragment plan's streams;
//   reset_sirius_cache and the profiler act on the whole process; sirius_read_parquet (an
//   internal target of gpu_execution's rewrite) reads a file with Sirius's own I/O, past the
//   server's file checks. They stay the administrator's: no grant opens them to a store user,
//   and sirius_read_parquet reads s3:// objects only, for everyone (SiriusReadParquetRefusal).
//
// - About fifty settings, which DuckDB lets any session change. Several act on the whole
//   process whatever the scope they are set in (the log backend, directory and level are
//   static in Sirius; spill compression), and some reach the host: the log directory and the
//   pinned-table compression-plan directory are paths Sirius opens with its own I/O, and the
//   exchange settings make it listen on every interface and dial peers. Every Sirius setting
//   needs MANAGE CONFIG (as SET GLOBAL does), in any scope; the ones that reach the host can be
//   set only in the server's startup SQL (IsSiriusStartupOnlySetting), for every session, the
//   administrator's included. Which settings are Sirius's is learnt as it loads (the options
//   an extension named sirius* registers while it loads), with the names of the build this
//   was written against as a floor.
#pragma once

#include <duckdb_v2.h>

#include <optional>
#include <string>
#include <string_view>

namespace duckdb {
class DatabaseInstance;
}

namespace hardcharger::authz {

// The name Sirius's s3:// file system gives itself.
inline constexpr char kSiriusS3FileSystem[] = "SiriusHttpFS";

// Before the init SQL: on every extension load from then on, take Sirius's s3:// file system
// out of DuckDB's (if the load registered it) and record which settings the extension added.
void InstallSiriusPolicy(duckdb_v2_instance_handle instance);
void InstallSiriusPolicy(duckdb::DatabaseInstance& database);

// Takes Sirius's s3:// file system out of the instance's file system if it is there; whether
// it was. (InstallSiriusPolicy does this on every load; here for the tests and the start.)
bool RemoveSiriusS3FileSystem(duckdb_v2_instance_handle instance);
bool RemoveSiriusS3FileSystem(duckdb::DatabaseInstance& database);

// `name` (any case) is a Sirius setting.
bool IsSiriusSetting(std::string_view name);

// `name` is a Sirius setting that reaches the host: a path Sirius opens itself, its process-wide
// log sink, or the exchange's listener and peers. Set only in the startup SQL.
bool IsSiriusStartupOnlySetting(std::string_view name);

// `name` (lower case) is a Sirius table function that stays the administrator's.
bool IsSiriusAdministratorOnlyFunction(std::string_view name);

// Why the parser refuses `sql`, if it calls sirius_read_parquet() with anything but one
// s3:// literal first (the only form Sirius's own rewrite produces).
std::optional<std::string> SiriusReadParquetRefusal(const std::string& sql);

// For the tests: record `name` as a setting the extension `extension` registered.
void RecordExtensionSettingForTesting(const std::string& name, const std::string& extension);

}  // namespace hardcharger::authz
