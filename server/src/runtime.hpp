// What every session of one server shares: its configuration, its identity,
// the session table, the statement queue, metrics, and the settings an admin can change
// while it runs (SET GLOBAL hardcharger.*).
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>

#include "admission.hpp"
#include "config.hpp"
#include "instance_info.hpp"
#include "logging.hpp"
#include "metrics.hpp"
#include "sessions.hpp"

namespace chargewire {
class Server;
}

namespace hardcharger {

class CatalogLog;
class HealthChecker;
class Instrumentation;

// What the instrumentation writer has done, for hardcharger_server_info() and /metrics.
struct InstrumentationStats {
  std::atomic<uint64_t> written{0};  // events written to the tables
  std::atomic<uint64_t> dropped{0};  // events never written: the queue was full, or left at the drain deadline
  std::atomic<uint64_t> failed{0};   // events whose write failed (logged)
  std::atomic<int64_t> queued{0};    // events waiting now
};

struct Runtime {
  Runtime(const Config& config, std::string instance_id)
      : config(config),
        instance_id(std::move(instance_id)),
        sessions(config.max_sessions),
        query_timeout_seconds(config.query_timeout_seconds),
        query_log_level(ParseLogLevel(config.query_log_level)),
        auth_log_level(ParseLogLevel(config.auth_log_level)),
        session_log_level(ParseLogLevel(config.session_log_level)),
        graceful_shutdown(config.graceful_shutdown),
        shutdown_grace_period_seconds(config.shutdown_grace_period_seconds),
        capture_query_profile(config.capture_query_profile == "detailed"   ? 2
                              : config.capture_query_profile == "standard" ? 1
                                                                           : 0),
        started_at(std::chrono::system_clock::now()) {
    admission.SetLimit(config.max_concurrent_statements);
    admission.SetMaxQueued(config.max_queued_statements);  // -1: 8 x the limit, as it changes
    admission.SetDefaultMaxQueueWaitSeconds(EffectiveMaxQueueWait(config.max_queue_wait));
  }

  const Config& config;
  const std::string instance_id;  // a UUID, new for every process

  SessionRegistry sessions;
  AdmissionController admission;
  Metrics metrics;
  HealthChecker* health = nullptr;  // set once the backend has started it
  Instrumentation* instrumentation = nullptr;  // set while instrumentation runs
  InstrumentationStats instrumentation_stats;  // kept after it stops
  CatalogLog* catalog_log = nullptr;           // set while catalog logging runs
  // The Chargewire server, for the SQL functions that see and cancel its sessions (set before
  // it starts; null in a process that serves nothing).
  chargewire::Server* server = nullptr;
  // What this instance is and runs on, collected at startup (the backend fills it).
  InstanceInfo instance_info;

  // Live-adjustable with SET GLOBAL.
  std::atomic<int64_t> query_timeout_seconds;
  std::atomic<LogLevel> query_log_level;
  const LogLevel auth_log_level;
  const LogLevel session_log_level;
  std::atomic<bool> graceful_shutdown;
  std::atomic<int64_t> shutdown_grace_period_seconds;
  std::atomic<bool> draining = false;
  // 0 off, 1 standard, 2 detailed (SET GLOBAL hardcharger.capture_query_profile).
  std::atomic<int> capture_query_profile;
  std::string scratch_dir;  // the server's private directory (query profiles)
  // Where new sessions start (default_catalog / default_schema, or the init SQL's final
  // USE, or the database's own catalog and schema): set once by the backend at startup.
  std::string default_catalog;
  std::string default_schema;

  const std::chrono::system_clock::time_point started_at;
};

}  // namespace hardcharger
