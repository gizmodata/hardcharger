// The access control store's connection and the thread that runs its statements (store.cc).
// A header of its own so the unit tests can drive it; nothing else includes it.
#pragma once

#include <arrow/result.h>
#include <arrow/status.h>

#include <chrono>
#include <condition_variable>
#include <deque>
#include <duckdb.hpp>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "authz/store.hpp"
#include "logging.hpp"

namespace hardcharger::authz {

namespace dd = duckdb;

// The store's DuckDB connection, which only a thread of its own uses, so that a store that
// stops answering holds no caller longer than the store timeout. A PostgreSQL that is
// paused, overloaded or cut off without a reset leaves libpq waiting in recv(), and
// nothing in DuckDB can interrupt that. So a caller that has waited the timeout is told
// the store is unavailable, and the statement is left to this thread. Whatever it was part
// of is then cancelled: when the statement returns, the thread rolls back the open
// transaction before it runs anything else, so a change a caller was told failed never
// commits (a COMMIT already sent may still land, and the caller is told so). The thread
// only ever holds what a statement owns (its SQL and values), never a caller's data, and
// keeps the DuckDB instance alive for as long as it runs. A statement that outlasts the
// timeout is logged when it does answer, with how long it took: a store that is slow (a
// commit waiting on a disk) is told apart from one that is down, which never answers. The
// caller's answer is settled first: the warning is written after the result is handed over,
// never in between (a caller past its deadline that takes the lock while the warning is
// written would be told the store timed out although its result was there).
class StoreLink {
 public:
  using Work = std::function<dd::unique_ptr<dd::QueryResult>(dd::Connection&)>;

  StoreLink(std::unique_ptr<dd::DuckDB> db, std::chrono::milliseconds timeout)
      : state_(std::make_shared<State>()), timeout_(timeout) {
    state_->timeout = timeout;
    state_->db = std::move(db);
    state_->conn = std::make_unique<dd::Connection>(*state_->db);
    thread_ = std::thread([state = state_] { Loop(*state); });
  }

  ~StoreLink() {
    std::unique_lock lock(state_->mutex);
    state_->stopping = true;
    state_->cv.notify_all();
    // A thread still waiting on a store that hangs is left behind: it owns all it uses.
    const bool finished =
        state_->cv.wait_for(lock, timeout_, [this] { return state_->finished; });
    lock.unlock();
    if (finished) {
      thread_.join();
    } else {
      thread_.detach();
    }
  }

  StoreLink(const StoreLink&) = delete;
  StoreLink& operator=(const StoreLink&) = delete;

  std::chrono::milliseconds timeout() const { return timeout_; }

  // For tests: called on the store's thread, holding the link's lock, when a statement has
  // returned and before its answer is settled (unit/store_test.cc forces the caller to wait
  // for the lock past its deadline with it).
  void SetBeforeHandoverForTesting(std::function<void()> hook) {
    std::lock_guard lock(state_->mutex);
    state_->before_handover = std::move(hook);
  }

  // Whether a statement has run for the whole timeout (one a caller gave up on included):
  // the store doesn't answer.
  bool Hung() const {
    std::lock_guard lock(state_->mutex);
    return state_->busy && std::chrono::steady_clock::now() - state_->busy_since >= timeout_;
  }

  // Runs `work` on the store's thread, waiting at most the timeout. Ok: its result.
  // `what` names the statement in the log (its first keyword: no values).
  arrow::Result<dd::unique_ptr<dd::QueryResult>> Call(Work work, std::string what = {}) {
    auto task = std::make_shared<Task>();
    task->work = std::move(work);
    task->what = std::move(what);
    std::unique_lock lock(state_->mutex);
    const auto now = std::chrono::steady_clock::now();
    // A statement that has hung for the whole timeout already: no use queueing behind it.
    if (state_->busy && now - state_->busy_since >= timeout_) return TimedOut();
    state_->queue.push_back(task);
    state_->cv.notify_all();
    if (!state_->cv.wait_until(lock, now + timeout_, [&] { return task->done; })) {
      task->abandoned = true;
      state_->rollback_pending = true;  // whatever transaction it was part of is cancelled
      state_->cv.notify_all();
      return TimedOut();
    }
    if (!task->error.empty()) {
      return arrow::Status::IOError("Hardcharger access-control store: statement failed: " + task->error);
    }
    return std::move(task->result);
  }

  arrow::Status TimedOut() const {
    return arrow::Status::IOError("Hardcharger ", kStoreUnavailable, ": timed out after ", Seconds(timeout_.count()),
                                  " s (rbac_store_timeout_seconds)");
  }

 private:
  static std::string Seconds(int64_t ms) {
    std::string seconds = std::to_string(ms / 1000);
    if (ms % 1000 >= 100) seconds += "." + std::to_string(ms % 1000 / 100);
    return seconds;
  }

  struct Task {
    Work work;
    std::string what;
    bool done = false;
    bool abandoned = false;
    dd::unique_ptr<dd::QueryResult> result;
    std::string error;
  };
  struct State {
    std::chrono::milliseconds timeout{0};
    std::unique_ptr<dd::DuckDB> db;
    std::unique_ptr<dd::Connection> conn;
    std::mutex mutex;
    std::condition_variable cv;
    std::deque<std::shared_ptr<Task>> queue;
    bool stopping = false;
    bool finished = false;
    bool rollback_pending = false;
    bool busy = false;  // running a statement (since busy_since)
    std::chrono::steady_clock::time_point busy_since;
    std::function<void()> before_handover;  // tests only
    ~State() {
      conn.reset();
      db.reset();
    }
  };

  // A statement that took the whole timeout or longer, said once its answer is settled. Called
  // with the lock held, after the handover; logs without it. Not while the link is going away
  // (its thread may outlive the server's logging).
  static void NoteSlow(State& state, std::unique_lock<std::mutex>& lock, std::chrono::steady_clock::duration took,
                       const std::string& what, bool caller_gave_up) {
    if (took < state.timeout || state.stopping) return;
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(took).count();
    lock.unlock();
    Log(LogLevel::kWarning,
        caller_gave_up ? "the access control store answered after its caller had given up: it is slow, not down"
                       : "the access control store took the whole rbac_store_timeout_seconds to answer",
        {{"statement", what.empty() ? std::string("statement") : what},
         {"seconds", Seconds(ms)},
         {"rbac_store_timeout_seconds", Seconds(state.timeout.count())}});
    lock.lock();
  }

  static void Loop(State& state) {
    std::unique_lock lock(state.mutex);
    while (true) {
      state.cv.wait(lock, [&] { return state.stopping || state.rollback_pending || !state.queue.empty(); });
      if (state.rollback_pending) {
        // A caller gave up on a statement of this connection's transaction: undo it
        // before anything else runs (outside a transaction this only errs).
        state.rollback_pending = false;
        state.busy = true;
        state.busy_since = std::chrono::steady_clock::now();
        const auto since = state.busy_since;
        lock.unlock();
        try {
          (void)state.conn->Query("ROLLBACK");
        } catch (const std::exception&) {
        }
        const auto took = std::chrono::steady_clock::now() - since;
        lock.lock();
        state.busy = false;
        NoteSlow(state, lock, took, "ROLLBACK", false);
        continue;
      }
      if (state.stopping) break;
      auto task = std::move(state.queue.front());
      state.queue.pop_front();
      if (task->abandoned) continue;
      state.busy = true;
      state.busy_since = std::chrono::steady_clock::now();
      const auto since = state.busy_since;
      lock.unlock();
      dd::unique_ptr<dd::QueryResult> result;
      std::string error;
      try {
        result = task->work(*state.conn);
      } catch (const std::exception& e) {
        error = e.what();
        if (error.empty()) error = "unknown error";
      }
      lock.lock();
      if (state.before_handover) state.before_handover();
      const auto took = std::chrono::steady_clock::now() - since;
      // The answer first, exactly as without the warning: the caller gets the result if it
      // has not given up by now.
      state.busy = false;
      const bool caller_gave_up = task->abandoned;
      if (!caller_gave_up) {
        task->result = std::move(result);
        task->error = std::move(error);
        task->done = true;
        state.cv.notify_all();
      } else {
        lock.unlock();
        result.reset();
        lock.lock();
      }
      NoteSlow(state, lock, took, task->what, caller_gave_up);
    }
    state.finished = true;
    state.cv.notify_all();
  }

  std::shared_ptr<State> state_;
  std::chrono::milliseconds timeout_;
  std::thread thread_;
};

}  // namespace hardcharger::authz
