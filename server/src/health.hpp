// The health check (spec §3.2), run in the background so a probe never waits on the
// database: a thread runs the configured query every interval on its own connection,
// and a probe reads the last outcome. A query that hangs makes the last outcome go
// stale, and a stale outcome is a failed one.
#pragma once

#include <duckdb_v2.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

#include "duckdb_v2.hpp"

namespace hardcharger {

class HealthChecker {
 public:
  HealthChecker(duckdb_v2_instance_handle instance, std::string query, std::chrono::seconds interval,
                std::chrono::seconds staleness);
  ~HealthChecker();

  // Runs the first check and starts the thread.
  void Start();
  // Stops the thread, interrupting a check that hangs; returns promptly either way.
  void Stop();

  // Throws (with the reason) unless the last check passed recently enough.
  void Check() const;
  bool Healthy() const;

 private:
  void RunOnce();
  void Loop();

  const std::string query_;
  const std::chrono::seconds interval_;
  const std::chrono::seconds staleness_;
  warpduckdb::Connection connection_;

  mutable std::mutex mutex_;
  std::condition_variable wake_;
  bool stopping_ = false;
  bool exited_ = false;
  bool last_ok_ = false;
  std::string last_error_ = "no health check has run yet";
  std::chrono::steady_clock::time_point last_finished_{};
  std::atomic<bool> running_query_ = false;
  std::thread thread_;
};

}  // namespace hardcharger
