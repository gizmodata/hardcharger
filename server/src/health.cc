#include "health.hpp"

#include "duckdb_sql.hpp"
#include "logging.hpp"

namespace hardcharger {

HealthChecker::HealthChecker(duckdb_v2_instance_handle instance, std::string query,
                             std::chrono::seconds interval, std::chrono::seconds staleness)
    : query_(std::move(query)), interval_(interval), staleness_(staleness) {
  chargewire_duckdb::Call([&](auto* err) { return duckdb_v2_connection_create(instance, connection_.out(), err); });
}

HealthChecker::~HealthChecker() { Stop(); }

void HealthChecker::RunOnce() {
  bool ok = true;
  std::string error;
  running_query_ = true;
  const auto started = std::chrono::steady_clock::now();
  try {
    chargewire_duckdb::RunSql(connection_.get(), query_);
  } catch (const std::exception& e) {
    ok = false;
    error = e.what();
  }
  last_duration_seconds_ = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  running_query_ = false;
  MutexLock lock(mutex_);
  if (ok != last_ok_ || !reported_) {
    reported_ = true;
    if (ok) {
      Log(LogLevel::kInfo, "health check passing");
    } else {
      Log(LogLevel::kWarning, "health check failing", {{"error", error}});
    }
  }
  last_ok_ = ok;
  last_error_ = error;
  last_finished_ = std::chrono::steady_clock::now();
}

void HealthChecker::Start() {
  {
    MutexLock lock(mutex_);
    last_error_ = "the first health check has not finished yet";
    last_finished_ = std::chrono::steady_clock::now();  // staleness counts from the start
  }
  // The first check runs on the thread too: one that hangs must not hold up startup.
  thread_ = std::thread([this] { Loop(); });
}

void HealthChecker::Loop() {
  UniqueMutexLock lock(mutex_);
  while (!stopping_) {
    lock.Unlock();
    RunOnce();
    lock.Lock();
    wake_.wait_for(lock.native(), interval_, [this]() HC_REQUIRES(mutex_) { return stopping_; });
  }
  exited_ = true;
  wake_.notify_all();
}

void HealthChecker::Stop() {
  UniqueMutexLock lock(mutex_);
  if (!thread_.joinable()) return;
  stopping_ = true;
  wake_.notify_all();
  // A hung check holds the thread: interrupt it, and give up waiting after 5 s.
  for (int i = 0; i < 50 && !exited_; i++) {
    if (running_query_) duckdb_v2_connection_interrupt(connection_.get(), nullptr);
    wake_.wait_for(lock.native(), std::chrono::milliseconds(100), [this]() HC_REQUIRES(mutex_) { return exited_; });
  }
  if (exited_) {
    lock.Unlock();
    thread_.join();
    return;
  }
  Log(LogLevel::kWarning, "health check did not stop within 5 s; leaving it behind");
  thread_.detach();
  // The thread may still use the connection, so it must outlive this object.
  connection_.release();
}

void HealthChecker::Check() const {
  MutexLock lock(mutex_);
  const auto age = std::chrono::steady_clock::now() - last_finished_;
  if (age > staleness_) {
    throw std::runtime_error("the last health check finished " +
                             std::to_string(std::chrono::duration_cast<std::chrono::seconds>(age).count()) +
                             " s ago");
  }
  if (!last_ok_) throw std::runtime_error("health check failed: " + last_error_);
}

bool HealthChecker::Healthy() const {
  try {
    Check();
    return true;
  } catch (const std::exception&) {
    return false;
  }
}

}  // namespace hardcharger
