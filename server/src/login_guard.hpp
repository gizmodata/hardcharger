// The login guard: limits on password guessing, in front of every password check (the
// Chargewire password grant, the web UI's sign-in, hardcharger-server --verify-credentials).
//
// Three counters of failed password sign-ins, each forgetting its failures once
// login_failure_window_seconds have passed since its last one:
//  - per (user, client address): after login_lockout_threshold failures the pair is locked
//    for login_lockout_seconds. While locked, every attempt of that user from that address
//    is refused before the password is checked, the right password included. Other
//    addresses are not affected, so nobody can lock an account out from everywhere.
//  - per user, across addresses: after login_user_backoff_after failures, a growing delay
//    (login_backoff_base_seconds, doubling with each further failure, at most
//    login_backoff_max_seconds) between attempts: one arriving sooner is refused. Never a
//    lock. Not applied to an address the user signed in from successfully within
//    login_known_address_days, so many addresses guessing can't keep a user out of the
//    places it signs in from.
//  - per client address, across users: the same growing delay after
//    login_address_backoff_after failures (one address spraying many names).
// A refusal is "429 rate_limited" with Retry-After, the same whether the user exists or not
// (an unknown name is counted like a known one). A successful sign-in resets its pair.
//
// Every failure, success and administrator clear is also a record in the access-control
// store when there is one (authz/login_events.hpp): each replica (and --verify-credentials)
// writes its own and reads the others' about once a second, so all of them reach the same
// counts. Decisions are made from memory: a guessing client never waits on the store, and
// a store that doesn't answer leaves each replica with its own counts (logged).
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "authz/login_events.hpp"

namespace hardcharger {

struct LoginGuardSettings {
  int64_t lockout_threshold = 5;  // login_lockout_threshold; 0: pairs never lock
  int64_t lockout_seconds = 900;  // login_lockout_seconds
  int64_t failure_window_seconds = 900;  // login_failure_window_seconds
  int64_t user_backoff_after = 5;      // login_user_backoff_after; 0: no per-user delay
  int64_t address_backoff_after = 20;  // login_address_backoff_after; 0: no per-address delay
  int64_t backoff_base_seconds = 1;    // login_backoff_base_seconds
  int64_t backoff_max_seconds = 30;    // login_backoff_max_seconds; 0: no delays at all
  int64_t known_address_days = 30;     // login_known_address_days; 0: no address is exempt
  // Bound on what is held in memory per kind (pairs, users, addresses). Past it the oldest
  // entries that aren't locked are forgotten.
  std::size_t max_entries = 100000;
  // Write a success that ends no failures (for other replicas' last_sign_in_at and known
  // addresses). The one-shot verifier doesn't: the server the credential then reaches does.
  bool record_routine_successes = true;
};

// Where the guard keeps its records beyond this process (the access-control store).
class LoginEventStore {
 public:
  virtual ~LoginEventStore() = default;
  // Each returns an empty string on success, else what went wrong (for the log).
  virtual std::string Append(const std::vector<authz::LoginEventRecord>& events) = 0;
  virtual std::string Since(int64_t since_ms, std::vector<authz::LoginEventRecord>& out) = 0;
  virtual std::string Prune(int64_t failures_before_ms, int64_t successes_before_ms) = 0;
};

class LoginGuard {
 public:
  using Clock = std::function<int64_t()>;  // milliseconds since the Unix epoch

  struct Decision {
    bool allowed = true;
    int64_t retry_after_seconds = 0;
    std::string reason;  // "locked", "user_backoff" or "address_backoff" when refused
  };

  // One row of hardcharger_login_locks().
  struct Entry {
    std::string scope;  // "pair", "user" or "address"
    std::string user_name;  // empty for "address"
    std::string client_address;  // empty for "user"
    int64_t failures = 0;
    int64_t last_failure_ms = 0;
    int64_t locked_until_ms = 0;  // pairs only; 0: not locked
    int64_t blocked_until_ms = 0;  // when the next attempt is accepted (lock or back-off); 0: now
  };

  struct UserSummary {
    std::optional<int64_t> last_sign_in_ms;
    int64_t failures = 0;
    std::optional<int64_t> last_failure_ms;
  };

  struct Counts {
    int64_t failures = 0;          // failed password checks
    int64_t locks = 0;             // pairs locked (here)
    int64_t throttled_locked = 0;  // attempts refused before the check, by reason
    int64_t throttled_user = 0;
    int64_t throttled_address = 0;
    int64_t active_locks = 0;      // pairs locked now
  };

  // `origin`: this instance's id (its records are skipped when read back). `store`: may be
  // null (memory only). `clock`: null for the system clock.
  LoginGuard(LoginGuardSettings settings, std::string origin, std::shared_ptr<LoginEventStore> store = nullptr,
             Clock clock = nullptr);
  ~LoginGuard();
  LoginGuard(const LoginGuard&) = delete;
  LoginGuard& operator=(const LoginGuard&) = delete;

  const LoginGuardSettings& settings() const { return settings_; }

  // Reads the store's records once (at startup, and the one-shot verifier). Returns what
  // went wrong, or empty.
  std::string Load();
  // A background thread that writes this instance's records, reads the others', and ends
  // expired locks (logged) about every `interval`. Stop() (and the destructor) ends it.
  void Start(std::chrono::milliseconds interval = std::chrono::milliseconds(1000));
  void Stop();
  // One round of that work, now (tests and the verifier).
  void Sync();
  // Why records are still waiting to be written (the store's last error), or empty.
  std::string PendingProblem();

  // Before a password is checked: whether to check it at all.
  Decision Check(std::string_view user_name, std::string_view client_address);
  void RecordFailure(std::string_view user_name, std::string_view client_address);
  void RecordSuccess(std::string_view user_name, std::string_view client_address);
  // An administrator clears what it names: a pair and its user's delay (both), a user's
  // pairs and delay (user only), an address's pairs and delay (address only), or everything
  // (neither). Returns
  // how many entries it cleared. `by` names the administrator for the log and audit.
  int64_t Clear(const std::optional<std::string>& user_name, const std::optional<std::string>& client_address,
                const std::string& by);

  std::vector<Entry> List();
  UserSummary ForUser(const std::string& user_name);
  Counts counts();

  // The keys the guard counts under (exposed for tests). A user name folds ASCII case, as
  // the store's names do; a long one is a hash. An address loses its port and brackets, an
  // IPv4-mapped IPv6 address is the IPv4 one, and an IPv6 address counts as its /64 (one
  // host can have a whole /64).
  static std::string UserKey(std::string_view user_name);
  static std::string AddressKey(std::string_view client_address);

 private:
  struct Counter {
    int64_t failures = 0;
    int64_t last_failure_ms = 0;
  };
  struct Pair {
    Counter counter;
    int64_t locked_until_ms = 0;
    int64_t last_success_ms = 0;
    std::string user_name;
    bool locked_here = false;  // this instance logged the lock: it logs the end too
  };
  struct User {
    Counter counter;
    int64_t last_success_ms = 0;
    std::string user_name;
  };
  using PairKey = std::pair<std::string, std::string>;

  int64_t Now() const;
  int64_t Current(const Counter& counter, int64_t now) const;
  void Add(Counter& counter, int64_t at) const;
  // When the next attempt is accepted under `counter`'s back-off after `after` failures.
  int64_t BackoffUntil(const Counter& counter, int64_t after, int64_t now) const;
  // Applies a record (this instance's or another's). Returns whether it locked a pair.
  bool ApplyLocked(const authz::LoginEventRecord& event);
  int64_t ClearLocked(const std::string& user_key, const std::string& address, int64_t at,
                      std::vector<std::pair<std::string, std::string>>* ended);
  void QueueLocked(authz::LoginEventRecord event);
  void PruneLocked(int64_t now);
  void Loop(std::chrono::milliseconds interval);
  bool Known(const Pair* pair, int64_t now) const;

  const LoginGuardSettings settings_;
  const std::string origin_;
  const std::shared_ptr<LoginEventStore> store_;
  const Clock clock_;

  std::mutex mutex_;
  std::map<PairKey, Pair> pairs_;
  std::map<std::string, User> users_;
  std::map<std::string, Counter> addresses_;
  std::vector<authz::LoginEventRecord> pending_;  // to write
  std::map<std::string, int64_t> seen_;           // records read back: id -> at_ms
  int64_t read_up_to_ms_ = 0;                      // newest record read
  int64_t last_prune_ms_ = 0;
  int64_t last_store_prune_ms_ = 0;
  bool store_failing_ = false;
  std::string last_problem_;
  Counts counts_;

  std::mutex loop_mutex_;
  std::condition_variable loop_cv_;
  bool stopping_ = false;
  std::thread thread_;
};

// The store's records through access control (null `access`: none).
namespace authz {
class AccessControl;
}
std::shared_ptr<LoginEventStore> StoreLoginEvents(std::shared_ptr<authz::AccessControl> access);

struct Config;
// The login_* settings.
LoginGuardSettings LoginGuardSettingsFrom(const Config& config);

}  // namespace hardcharger
