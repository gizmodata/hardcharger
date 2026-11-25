#include "authz/sirius.hpp"

#include <duckdb.hpp>
#include <duckdb/common/error_data.hpp>
#include <duckdb/common/multi_file/multi_file_list.hpp>
#include <duckdb/common/multi_file/multi_file_states.hpp>
#include <duckdb/common/string_util.hpp>
#include <duckdb/common/virtual_file_system.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/client_context_state.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database.hpp>
#include <duckdb/optimizer/optimizer_extension.hpp>
#include <duckdb/planner/extension_callback.hpp>
#include <duckdb/planner/operator/logical_get.hpp>

#include <algorithm>
#include <atomic>
#include <mutex>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "logging.hpp"
#include "sql_lex.hpp"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;

std::string Lower(std::string_view s) {
  std::string out(s);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

// The settings Sirius build 1290c3ea84 (DuckDB v2.0.1's sirius / sirius_cuda13 pair) registers,
// read from duckdb_settings() with and without it loaded. A floor: whatever a newer build adds is
// learnt as it loads (SettingOwners).
const std::unordered_set<std::string>& KnownSiriusSettings() {
  static const std::unordered_set<std::string> kNames = {"admission_bytes_per_gpu",
                                                         "avg_variable_column_bytes",
                                                         "dynamic_filter_domain_coverage_threshold",
                                                         "dynamic_filter_inlist_max_l2_fraction",
                                                         "dynamic_filter_keep_threshold",
                                                         "enable_compressed_materialization",
                                                         "enable_duckdb_fallback",
                                                         "enable_fused_scan_filter",
                                                         "enable_late_materialization",
                                                         "enable_low_cardinality_groupby",
                                                         "enable_regex_jit_impl",
                                                         "enable_runtime_size_estimation",
                                                         "expression_evaluator_strategy",
                                                         "fused_scan_diagnostics",
                                                         "fused_scan_full_route_max_selectivity",
                                                         "fused_scan_index_walk_max_selectivity",
                                                         "fused_scan_max_membership_sources",
                                                         "fused_scan_max_selectivity",
                                                         "gpu_execution",
                                                         "hash_partition_bytes",
                                                         "late_materialization_count_defer",
                                                         "late_materialization_min_boundaries",
                                                         "late_materialization_min_value_compressed",
                                                         "late_materialization_min_value_x_boundaries",
                                                         "like_swar_fastpath",
                                                         "mark_join_build_switch_ratio",
                                                         "max_broadcast_join_size",
                                                         "max_build_hash_table_bytes",
                                                         "max_build_probe_resident_bytes",
                                                         "max_sort_partition_bytes",
                                                         "max_sort_partition_memory_fraction",
                                                         "parquet_footer_statistics",
                                                         "pin_table_compression_max_compressed_fraction",
                                                         "pin_table_compression_min_batch_size_bytes",
                                                         "pin_table_compression_min_table_device_fraction",
                                                         "pin_table_compression",
                                                         "pin_table_input_compression_plan_dir",
                                                         "pin_table_prove_unique_keys",
                                                         "pin_table_unique_exact_max_bytes",
                                                         "sirius_exchange_host_staging",
                                                         "sirius_exchange_peers",
                                                         "sirius_exchange_rank",
                                                         "sirius_log_backend",
                                                         "sirius_log_dir",
                                                         "sirius_log_flush_seconds",
                                                         "sirius_log_level",
                                                         "sort_sample_bytes",
                                                         "spill_compression"};
  return kNames;
}

// Which extension registered each setting the server has seen an extension load register.
struct SettingOwners {
  std::mutex mutex;
  std::unordered_map<std::string, std::string> owner;  // setting (lower case) -> extension (lower case)
};
SettingOwners& Owners() {
  static auto* owners = new SettingOwners();  // never destroyed: read by statements until exit
  return *owners;
}

bool IsSiriusExtension(std::string_view extension) { return Lower(extension).starts_with("sirius"); }

std::set<std::string> ExtensionSettingNames(dd::DatabaseInstance& db) {
  std::set<std::string> names;
  for (const auto& entry : dd::DBConfig::GetConfig(db).GetExtensionSettings()) {
    names.insert(Lower(entry.first.GetIdentifierName()));
  }
  return names;
}

// One load in progress on this thread (an extension may load another while it loads: Sirius's
// loader loads its CUDA backend), with the settings there were when it began.
struct LoadFrame {
  std::string extension;
  std::set<std::string> before;
};
thread_local std::vector<LoadFrame> loads;

void Record(const std::string& setting, const std::string& extension) {
  auto& owners = Owners();
  std::lock_guard<std::mutex> lock(owners.mutex);
  owners.owner.emplace(setting, Lower(extension));  // the innermost load recorded it first
}

// The file systems taken out of DuckDB's: kept, never destroyed (an extension's code is never
// unloaded, and nothing must run its destructor while a statement might still hold a handle).
std::mutex removed_mutex;
std::vector<dd::FileSystem*>& Removed() {
  static auto* removed = new std::vector<dd::FileSystem*>();
  return *removed;
}

bool RemoveFrom(dd::DatabaseInstance& db) {
  auto& vfs = dd::DBConfig::GetConfig(db).file_system;
  if (!vfs) return false;
  const auto names = vfs->ListSubSystems();
  if (std::find(names.begin(), names.end(), kSiriusS3FileSystem) == names.end()) return false;
  auto removed = vfs->ExtractSubSystem(kSiriusS3FileSystem);
  if (!removed) return false;
  {
    std::lock_guard<std::mutex> lock(removed_mutex);
    Removed().push_back(removed.release());
  }
  Log(LogLevel::kInfo,
      "Sirius's s3:// file system (read-only, and for GPU scans only) is replaced by DuckDB's httpfs: S3 writes, "
      "DuckLake and COPY TO s3://, and CPU reads of S3 go through httpfs; GPU scans of S3 keep reading with Sirius's "
      "own client");
  return true;
}

class SiriusLoadPolicy final : public dd::ExtensionCallback {
 public:
  void OnBeginExtensionLoad(dd::DatabaseInstance& db, const dd::string& name) override {
    loads.push_back({name, ExtensionSettingNames(db)});
  }
  void OnExtensionLoaded(dd::DatabaseInstance& db, const dd::string& name) override { Finish(db, name); }
  void OnExtensionLoadFail(dd::DatabaseInstance& db, const dd::string& name, const dd::ErrorData&) override {
    Finish(db, name);  // a load that failed half-way may have registered either
  }

 private:
  static void Finish(dd::DatabaseInstance& db, const std::string& name) {
    // The frame of this load: the innermost of its name (a load that threw past its own end
    // left a frame behind, which this drops too).
    auto frame = std::find_if(loads.rbegin(), loads.rend(),
                              [&](const LoadFrame& f) { return dd::StringUtil::CIEquals(f.extension, name); });
    // (Loaded under an alias: DuckDB reports the alias at the end, the name at the start.)
    if (frame == loads.rend() && !loads.empty()) frame = loads.rbegin();
    if (frame != loads.rend()) {
      // Owned by the name the load began with, or by a sirius* name of either.
      const std::string owner = IsSiriusExtension(name) ? name : frame->extension;
      for (const auto& setting : ExtensionSettingNames(db)) {
        if (!frame->before.contains(setting)) Record(setting, owner);
      }
      loads.erase(std::next(frame).base(), loads.end());
    }
    RemoveFrom(db);
  }
};

// ---- S3 reads Sirius would refuse ----
//
// Sirius takes every plan its session allows (gpu_execution on), and when the GPU plan can't be built or fails, it
// replays the statement on DuckDB's CPU engine, except when the plan reads s3://: then the statement fails ("S3 CPU
// fallback is not supported"), whoever serves s3:// (plan_source_policy.cpp, sirius_context.cpp). Sirius scans only
// its connectors' table functions; any other one that reads files on S3 (read_csv, read_json, a DuckLake or Delta
// table's scan) always fails that way. Such a statement is run on the CPU from the start: gpu_execution is off for
// it alone (the session's value, put back when the statement ends), so Sirius leaves the plan to DuckDB and httpfs
// reads S3. GPU scans of S3 Parquet are not touched.

// The table functions Sirius scans on the GPU (src/planner/connector_registry.cpp, build 1290c3ea84).
bool SiriusScans(const std::string& function) {
  static const std::unordered_set<std::string> kNames = {
      "seq_scan", "parquet_scan", "read_parquet", "sirius_read_parquet", "iceberg_scan", "sirius_stream_source"};
  return kNames.contains(Lower(function));
}

bool IsS3(const std::string& path) { return Lower(path.substr(0, 5)) == "s3://"; }

// The plan reads S3 through a table function Sirius can't scan, as Sirius itself classifies it (a multi-file scan
// whose bound file list names s3://).
bool ReadsS3PastSirius(const dd::LogicalOperator& op) {
  if (op.type == dd::LogicalOperatorType::LOGICAL_GET) {
    const auto& get = op.Cast<dd::LogicalGet>();
    if (!SiriusScans(get.function.GetName().GetIdentifierName())) {
      if (const auto* files = dynamic_cast<const dd::MultiFileBindData*>(get.bind_data.get());
          files && files->file_list) {
        for (const auto& file : files->file_list->GetDisplayFileList(dd::optional_idx(64))) {
          if (IsS3(file.path)) return true;
        }
      }
    }
  }
  for (const auto& child : op.children) {
    if (child && ReadsS3PastSirius(*child)) return true;
  }
  return false;
}

constexpr char kRouteStateName[] = "hardcharger_sirius_cpu_route";

// The session's own gpu_execution while one statement runs with it off, put back when the statement's query ends.
class SiriusCpuRoute final : public dd::ClientContextState {
 public:
  explicit SiriusCpuRoute(dd::idx_t setting) : setting_(setting) {}
  void TurnOff(dd::ClientContext& context) {
    auto& settings = context.config.user_settings;
    if (!active_) {
      had_ = settings.IsSet(setting_);
      if (had_) {
        settings.TryGetSetting(dd::DBConfig::GetConfig(context).user_settings, setting_, previous_);
      }
      active_ = true;
    }
    settings.SetUserSetting(setting_, dd::Value::BOOLEAN(false));
  }
  void QueryEnd(dd::ClientContext& context, dd::optional_ptr<dd::ErrorData>) override {
    if (!active_) return;
    auto& settings = context.config.user_settings;
    if (had_) {
      settings.SetUserSetting(setting_, previous_);
    } else {
      settings.ClearSetting(setting_);
    }
    active_ = false;
  }

 private:
  const dd::idx_t setting_;
  bool active_ = false;
  bool had_ = false;
  dd::Value previous_;
};

std::atomic<bool> route_logged = false;

void RouteS3PastSirius(dd::OptimizerExtensionInput& input, dd::unique_ptr<dd::LogicalOperator>& plan) {
  if (!plan) return;
  auto& context = input.context;
  dd::ExtensionOption option;
  if (!dd::DBConfig::GetConfig(context).TryGetExtensionOption(dd::Identifier("gpu_execution"), option)) return;
  dd::Value on;
  if (!context.TryGetCurrentSetting("gpu_execution", on) || on.IsNull() || !on.GetValue<bool>()) return;
  if (!ReadsS3PastSirius(*plan)) return;
  auto state = context.registered_state->GetOrCreate<SiriusCpuRoute>(kRouteStateName, option.setting_index.GetIndex());
  state->TurnOff(context);
  if (!route_logged.exchange(true)) {
    Log(LogLevel::kInfo,
        "a statement reads S3 through a scan Sirius doesn't run on the GPU (read_csv, a DuckLake table, ...), which "
        "Sirius would refuse to run on the CPU: such statements run on DuckDB's CPU engine (logged once)");
  }
}

}  // namespace

void InstallSiriusPolicy(dd::DatabaseInstance& database) {
  auto& config = dd::DBConfig::GetConfig(database);
  dd::ExtensionCallback::Register(config, dd::make_shared_ptr<SiriusLoadPolicy>());
  // Before Sirius's own optimizer hooks, which register when it loads: they read gpu_execution after this one.
  dd::OptimizerExtension route;
  route.pre_optimize_function = RouteS3PastSirius;
  dd::OptimizerExtension::Register(config, std::move(route));
  RemoveFrom(database);  // (an extension loaded before the server's own code ran)
}

void InstallSiriusPolicy(duckdb_v2_instance_handle instance) {
  InstallSiriusPolicy(*dd::capiv2::Convert(instance)->GetDatabase().instance);
}

bool RemoveSiriusS3FileSystem(dd::DatabaseInstance& database) { return RemoveFrom(database); }

bool RemoveSiriusS3FileSystem(duckdb_v2_instance_handle instance) {
  return RemoveFrom(*dd::capiv2::Convert(instance)->GetDatabase().instance);
}

bool IsSiriusSetting(std::string_view name) {
  const std::string lower = Lower(name);
  if (lower.starts_with("sirius_") || KnownSiriusSettings().contains(lower)) return true;
  auto& owners = Owners();
  std::lock_guard<std::mutex> lock(owners.mutex);
  const auto it = owners.owner.find(lower);
  return it != owners.owner.end() && IsSiriusExtension(it->second);
}

bool IsSiriusStartupOnlySetting(std::string_view name) {
  if (!IsSiriusSetting(name)) return false;
  const std::string lower = Lower(name);
  static const std::unordered_set<std::string> kNames = {"sirius_log_backend",
                                                         "sirius_log_dir",
                                                         "sirius_log_level",
                                                         "sirius_exchange_peers",
                                                         "sirius_exchange_rank",
                                                         "sirius_exchange_host_staging",
                                                         "pin_table_input_compression_plan_dir"};
  if (kNames.contains(lower)) return true;
  // A newer build's: anything named like a path, a log sink or the exchange (fail closed).
  for (const char* part : {"_dir", "_path", "_file", "exchange", "peer", "listen", "log_backend", "socket", "port"}) {
    if (lower.find(part) != std::string::npos) return true;
  }
  return false;
}

bool IsSiriusAdministratorOnlyFunction(std::string_view name) {
  static const std::unordered_set<std::string_view> kNames = {
      "gpu_execution",           "sirius_stream_source",  "pin_table",          "unpin_table",    "sirius_knn_search",
      "sirius_create_ann_index", "sirius_drop_ann_index", "reset_sirius_cache", "profiler_start", "profiler_stop",
      "sirius_read_parquet"};
  return kNames.contains(name);
}

std::optional<std::string> SiriusReadParquetRefusal(const std::string& sql) {
  static constexpr std::string_view kName = "sirius_read_parquet";
  // Most texts never name it: a case-insensitive search before any tokenizing.
  const auto found = std::search(sql.begin(), sql.end(), kName.begin(), kName.end(),
                                 [](char a, char b) { return std::tolower(static_cast<unsigned char>(a)) == b; });
  if (found == sql.end()) return std::nullopt;
  const std::string refusal =
      "sirius_read_parquet() is Sirius's internal target for read_parquet('s3://...') and reads files with Sirius's "
      "own I/O, past the server's file checks: on this server it takes one s3:// literal and nothing else. "
      "read_parquet() runs on the GPU as well.";
  const SqlTokens tokens = LexSql(sql);
  if (tokens.unreadable()) return refusal;  // (DuckDB refuses it too; never guess)
  std::vector<std::size_t> significant;
  for (std::size_t i = 0; i < tokens.size(); i++) {
    if (tokens.kind(i) != SqlTokenKind::kSpace && tokens.kind(i) != SqlTokenKind::kComment) significant.push_back(i);
  }
  auto text = [&](std::size_t k) { return tokens[significant[k]].text; };
  auto kind = [&](std::size_t k) { return tokens.kind(significant[k]); };
  for (std::size_t k = 0; k < significant.size(); k++) {
    std::string_view word = text(k);
    if (kind(k) == SqlTokenKind::kQuotedName && word.size() >= 2)
      word = word.substr(1, word.size() - 2);
    else if (kind(k) != SqlTokenKind::kWord)
      continue;
    if (Lower(word) != kName) continue;
    if (k + 1 >= significant.size() || text(k + 1) != "(") continue;  // named, not called
    // One plain '...' literal that starts s3://, then , or ).
    if (k + 3 >= significant.size() || kind(k + 2) != SqlTokenKind::kString) return refusal;
    const std::string_view literal = text(k + 2);
    if (literal.size() < 2 || literal.front() != '\'' || literal.back() != '\'') return refusal;
    if (!Lower(literal.substr(1, std::min<std::size_t>(5, literal.size() - 2))).starts_with("s3://")) return refusal;
    if (text(k + 3) != "," && text(k + 3) != ")") return refusal;
  }
  return std::nullopt;
}

void RecordExtensionSettingForTesting(const std::string& name, const std::string& extension) {
  Record(Lower(name), extension);
}

}  // namespace hardcharger::authz
