// What every session of one server shares: its configuration and license, its identity,
// the session table, the statement queue, metrics, and the settings an admin can change
// while it runs (SET GLOBAL gizmosql.*).
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <string>

#include "admission.hpp"
#include "config.hpp"
#include "license.hpp"
#include "logging.hpp"
#include "metrics.hpp"
#include "sessions.hpp"

namespace hardcharger {

class CatalogLog;
class HealthChecker;
class Instrumentation;

struct Runtime {
  Runtime(const Config& config, const License& license, std::string instance_id)
      : config(config),
        license(license),
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
    admission.SetMaxQueued(config.max_queued_statements < 0 ? 8 * config.max_concurrent_statements
                                                            : config.max_queued_statements);
    admission.SetDefaultMaxQueueWaitSeconds(config.max_queue_wait < 0 ? 300 : config.max_queue_wait);
  }

  const Config& config;
  const License& license;
  const std::string instance_id;  // a UUID, new for every process

  SessionRegistry sessions;
  AdmissionController admission;
  Metrics metrics;
  HealthChecker* health = nullptr;  // set once the backend has started it
  Instrumentation* instrumentation = nullptr;  // set while instrumentation runs
  CatalogLog* catalog_log = nullptr;           // set while catalog logging runs

  // Live-adjustable with SET GLOBAL.
  std::atomic<int64_t> query_timeout_seconds;
  std::atomic<LogLevel> query_log_level;
  const LogLevel auth_log_level;
  const LogLevel session_log_level;
  std::atomic<bool> graceful_shutdown;
  std::atomic<int64_t> shutdown_grace_period_seconds;
  std::atomic<bool> draining = false;
  // 0 off, 1 standard, 2 detailed (SET GLOBAL gizmosql.capture_query_profile).
  std::atomic<int> capture_query_profile;
  std::string scratch_dir;  // the server's private directory (query profiles)

  const std::chrono::system_clock::time_point started_at;
};

}  // namespace hardcharger
