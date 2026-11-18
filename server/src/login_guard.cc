#include "login_guard.hpp"

#include <openssl/evp.h>
#include <openssl/rand.h>

#include <algorithm>
#include <boost/asio/ip/address.hpp>
#include <cstring>
#include <ctime>

#include "audit.hpp"
#include "authz/authz.hpp"
#include "config.hpp"
#include "logging.hpp"

namespace hardcharger {

namespace {

constexpr int64_t kSecond = 1000;
constexpr int64_t kDay = 86400 * kSecond;
// How long a pair's last success is kept (hardcharger_users()' last_sign_in_at, and the
// known-address exemption when login_known_address_days is longer).
constexpr int64_t kSuccessRetention = 400 * kDay;
// A pair's success is written again (for other replicas' last_sign_in_at and known
// addresses) when the last one written is older than this; at once when it ends failures.
constexpr int64_t kSuccessRefresh = 300 * kSecond;
// Records are read back from this far before the newest one read: another replica's
// record may commit a little after its time stamp, or its clock may run behind.
constexpr int64_t kReadOverlap = 15 * kSecond;
constexpr int64_t kMemoryPruneEvery = 60 * kSecond;
constexpr int64_t kStorePruneEvery = 600 * kSecond;
constexpr std::size_t kMaxPending = 10000;
constexpr std::size_t kMaxNameBytes = 256;

std::string Hex(const unsigned char* bytes, std::size_t size) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(size * 2);
  for (std::size_t i = 0; i < size; ++i) {
    out += kHex[bytes[i] >> 4];
    out += kHex[bytes[i] & 15];
  }
  return out;
}

std::string NewEventId() {
  unsigned char bytes[16];
  if (RAND_bytes(bytes, sizeof bytes) != 1) {
    const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
    for (std::size_t i = 0; i < sizeof bytes; ++i) bytes[i] = static_cast<unsigned char>(now >> ((i % 8) * 8));
  }
  return Hex(bytes, sizeof bytes);
}

// A client's name for logs and listings: cut at 256 bytes.
std::string DisplayName(std::string_view name) {
  if (name.size() <= kMaxNameBytes) return std::string(name);
  return std::string(name.substr(0, kMaxNameBytes)) + "...";
}

std::string Time(int64_t ms) {
  const std::time_t seconds = static_cast<std::time_t>(ms / 1000);
  std::tm tm{};
#ifdef _WIN32
  gmtime_s(&tm, &seconds);
#else
  gmtime_r(&seconds, &tm);
#endif
  char buffer[32];
  std::strftime(buffer, sizeof buffer, "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buffer;
}

int64_t CeilSeconds(int64_t ms) { return std::max<int64_t>(1, (ms + 999) / 1000); }

}  // namespace

std::string LoginGuard::UserKey(std::string_view user_name) {
  std::string folded(user_name);
  for (auto& c : folded) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  if (folded.size() <= kMaxNameBytes) return folded;
  unsigned char digest[32];
  unsigned int length = 0;
  EVP_Digest(folded.data(), folded.size(), digest, &length, EVP_sha256(), nullptr);
  return "sha256:" + Hex(digest, sizeof digest);
}

std::string LoginGuard::AddressKey(std::string_view address) {
  std::string text(address);
  // "[v6]:port", "[v6]", "v4:port": the address alone.
  if (!text.empty() && text.front() == '[') {
    const auto close = text.find(']');
    text = text.substr(1, close == std::string::npos ? std::string::npos : close - 1);
  } else if (std::count(text.begin(), text.end(), ':') == 1) {
    text = text.substr(0, text.find(':'));
  }
  if (const auto zone = text.find('%'); zone != std::string::npos) text.resize(zone);
  boost::system::error_code ec;
  const auto parsed = boost::asio::ip::make_address(text, ec);
  if (!ec && parsed.is_v4()) return parsed.to_v4().to_string();
  if (!ec && parsed.is_v6()) {
    const auto v6 = parsed.to_v6();
    if (v6.is_v4_mapped()) return boost::asio::ip::make_address_v4(boost::asio::ip::v4_mapped, v6).to_string();
    auto bytes = v6.to_bytes();
    std::fill(bytes.begin() + 8, bytes.end(), 0);  // the /64
    return boost::asio::ip::address_v6(bytes).to_string() + "/64";
  }
  // Not an address (a test's name, a malformed forwarded value): as given, cut.
  return text.substr(0, 64);
}

LoginGuard::LoginGuard(LoginGuardSettings settings, std::string origin, std::shared_ptr<LoginEventStore> store,
                       Clock clock)
    : settings_(settings), origin_(std::move(origin)), store_(std::move(store)), clock_(std::move(clock)) {}

LoginGuard::~LoginGuard() { Stop(); }

int64_t LoginGuard::Now() const {
  if (clock_) return clock_();
  return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch())
      .count();
}

int64_t LoginGuard::Current(const Counter& counter, int64_t now) const {
  if (counter.failures == 0) return 0;
  if (now - counter.last_failure_ms > settings_.failure_window_seconds * kSecond) return 0;
  return counter.failures;
}

void LoginGuard::Add(Counter& counter, int64_t at) const {
  if (counter.failures > 0 && at - counter.last_failure_ms > settings_.failure_window_seconds * kSecond) {
    counter = {};
  }
  ++counter.failures;
  counter.last_failure_ms = std::max(counter.last_failure_ms, at);
}

int64_t LoginGuard::BackoffUntil(const Counter& counter, int64_t after, int64_t now) const {
  if (after <= 0 || settings_.backoff_max_seconds <= 0) return 0;
  const int64_t failures = Current(counter, now);
  if (failures < after) return 0;
  const int64_t doublings = std::min<int64_t>(failures - after, 40);
  int64_t delay = settings_.backoff_base_seconds;
  for (int64_t i = 0; i < doublings && delay < settings_.backoff_max_seconds; ++i) delay *= 2;
  delay = std::min(delay, settings_.backoff_max_seconds);
  return counter.last_failure_ms + delay * kSecond;
}

bool LoginGuard::Known(const Pair* pair, int64_t now) const {
  return pair != nullptr && settings_.known_address_days > 0 && pair->last_success_ms > 0 &&
         now - pair->last_success_ms <= settings_.known_address_days * kDay;
}

LoginGuard::Decision LoginGuard::Check(std::string_view user_name, std::string_view client_address) {
  const std::string user = UserKey(user_name);
  const std::string address = AddressKey(client_address);
  std::lock_guard lock(mutex_);
  const int64_t now = Now();
  Pair* pair = nullptr;
  if (!address.empty()) {
    if (auto it = pairs_.find({user, address}); it != pairs_.end()) pair = &it->second;
  }
  auto refuse = [&](int64_t until, const char* reason, int64_t& count) {
    ++count;
    return Decision{false, CeilSeconds(until - now), reason};
  };
  if (pair && pair->locked_until_ms > now) return refuse(pair->locked_until_ms, "locked", counts_.throttled_locked);
  // Attempts being checked right now count as failures until they end: at most the
  // threshold's worth of guesses are ever in the air for one pair.
  const bool lock_over = pair && pair->locked_until_ms > 0;  // (not > now: checked above)
  if (pair && settings_.lockout_threshold > 0 &&
      (lock_over ? 0 : Current(pair->counter, now)) + pair->in_flight >= settings_.lockout_threshold) {
    return refuse(now + kSecond, "locked", counts_.throttled_locked);
  }
  auto allow = [&]() -> Decision {
    if (!address.empty()) {
      Pair& held = pairs_[{user, address}];
      ++held.in_flight;
    }
    return {};
  };
  if (Known(pair, now)) return allow();
  if (auto it = users_.find(user); it != users_.end()) {
    const int64_t until = BackoffUntil(it->second.counter, settings_.user_backoff_after, now);
    if (until > now) return refuse(until, "user_backoff", counts_.throttled_user);
  }
  if (!address.empty()) {
    if (auto it = addresses_.find(address); it != addresses_.end()) {
      const int64_t until = BackoffUntil(it->second, settings_.address_backoff_after, now);
      if (until > now) return refuse(until, "address_backoff", counts_.throttled_address);
    }
  }
  return allow();
}

void LoginGuard::ReleaseLocked(const std::string& user_key, const std::string& address) {
  if (address.empty()) return;
  if (auto it = pairs_.find({user_key, address}); it != pairs_.end() && it->second.in_flight > 0) {
    --it->second.in_flight;
  }
}

void LoginGuard::Release(std::string_view user_name, std::string_view client_address) {
  const std::string user = UserKey(user_name);
  const std::string address = AddressKey(client_address);
  std::lock_guard lock(mutex_);
  ReleaseLocked(user, address);
}

bool LoginGuard::ApplyLocked(const authz::LoginEventRecord& e) {
  bool locked = false;
  if (e.kind == "clear") {
    ClearLocked(e.user_key, e.address, e.at_ms, nullptr);
    return false;
  }
  if (e.user_key.empty()) return false;
  const bool success = e.kind == "success";
  if (!success && e.kind != "failure") return false;  // a kind a later version writes
  if (!e.address.empty()) {
    Pair& pair = pairs_[{e.user_key, e.address}];
    if (!e.user_name.empty()) pair.user_name = e.user_name;
    if (success) {
      pair.counter = {};
      pair.locked_until_ms = 0;
      pair.last_success_ms = std::max(pair.last_success_ms, e.at_ms);
    } else {
      if (pair.locked_until_ms > 0 && pair.locked_until_ms <= e.at_ms) {  // that lock is over
        pair.counter = {};
        pair.locked_until_ms = 0;
      }
      Add(pair.counter, e.at_ms);
      if (settings_.lockout_threshold > 0 && pair.counter.failures >= settings_.lockout_threshold &&
          pair.locked_until_ms <= e.at_ms) {
        pair.locked_until_ms = e.at_ms + settings_.lockout_seconds * kSecond;
        locked = true;
      }
    }
    if (!success) Add(addresses_[e.address], e.at_ms);
  }
  User& user = users_[e.user_key];
  if (!e.user_name.empty()) user.user_name = e.user_name;
  if (success) {
    user.last_success_ms = std::max(user.last_success_ms, e.at_ms);
  } else {
    Add(user.counter, e.at_ms);
  }
  if (pairs_.size() > settings_.max_entries || users_.size() > settings_.max_entries ||
      addresses_.size() > settings_.max_entries) {
    PruneLocked(e.at_ms);
  }
  return locked;
}

int64_t LoginGuard::ClearLocked(const std::string& user_key, const std::string& address, int64_t at,
                                std::vector<std::pair<std::string, std::string>>* ended) {
  (void)at;
  int64_t cleared = 0;
  for (auto& [key, pair] : pairs_) {
    if ((!user_key.empty() && key.first != user_key) || (!address.empty() && key.second != address)) continue;
    if (pair.counter.failures == 0 && pair.locked_until_ms == 0) continue;
    if (ended && pair.locked_here && pair.locked_until_ms > 0) ended->emplace_back(pair.user_name, key.second);
    pair.counter = {};
    pair.locked_until_ms = 0;
    pair.locked_here = false;
    ++cleared;
  }
  // A named user's delay goes with any clear naming it (an administrator vouching for the
  // user at an address wants it to sign in there now); an address-only clear keeps them.
  if (!user_key.empty() || address.empty()) {
    for (auto& [key, user] : users_) {
      if ((!user_key.empty() && key != user_key) || user.counter.failures == 0) continue;
      user.counter = {};
      ++cleared;
    }
  }
  if (user_key.empty()) {
    for (auto it = addresses_.begin(); it != addresses_.end();) {
      if (!address.empty() && it->first != address) {
        ++it;
        continue;
      }
      ++cleared;
      it = addresses_.erase(it);
    }
  }
  return cleared;
}

void LoginGuard::QueueLocked(authz::LoginEventRecord event) {
  if (!store_) return;
  if (pending_.size() >= kMaxPending) pending_.erase(pending_.begin());
  pending_.push_back(std::move(event));
}

void LoginGuard::RecordFailure(std::string_view user_name, std::string_view client_address) {
  authz::LoginEventRecord event{NewEventId(), 0, "failure", UserKey(user_name), DisplayName(user_name),
                                AddressKey(client_address), origin_};
  int64_t locked_until = 0;
  int64_t failures = 0;
  {
    std::lock_guard lock(mutex_);
    event.at_ms = Now();
    ++counts_.failures;
    ReleaseLocked(event.user_key, event.address);
    if (ApplyLocked(event)) {
      Pair& pair = pairs_[{event.user_key, event.address}];
      pair.locked_here = true;
      locked_until = pair.locked_until_ms;
      failures = pair.counter.failures;
      ++counts_.locks;
    }
    QueueLocked(event);
  }
  if (locked_until > 0) {
    LogFields fields{{"kind", "authentication"},
                     {"status", "locked"},
                     {"user", event.user_name},
                     {"client_address", event.address},
                     {"failures", std::to_string(failures)},
                     {"locked_until", Time(locked_until)},
                     {"lock_seconds", std::to_string(settings_.lockout_seconds)}};
    Log(LogLevel::kWarning, "security: sign-ins of this user from this address are locked after failed attempts",
        fields);
    Audit(LogLevel::kWarning, "sign-in locked for a user and client address", std::move(fields));
  }
}

void LoginGuard::RecordSuccess(std::string_view user_name, std::string_view client_address) {
  authz::LoginEventRecord event{NewEventId(), 0, "success", UserKey(user_name), DisplayName(user_name),
                                AddressKey(client_address), origin_};
  std::lock_guard lock(mutex_);
  event.at_ms = Now();
  ReleaseLocked(event.user_key, event.address);
  bool write = true;
  if (!event.address.empty()) {
    if (auto it = pairs_.find({event.user_key, event.address}); it != pairs_.end()) {
      const Pair& pair = it->second;
      write = pair.counter.failures > 0 || pair.locked_until_ms > 0 ||
              event.at_ms - pair.last_success_ms > kSuccessRefresh;
    }
  } else if (auto it = users_.find(event.user_key); it != users_.end()) {
    write = event.at_ms - it->second.last_success_ms > kSuccessRefresh;
  }
  if (!settings_.record_routine_successes) {
    const auto it = event.address.empty() ? pairs_.end() : pairs_.find({event.user_key, event.address});
    write = it != pairs_.end() && (it->second.counter.failures > 0 || it->second.locked_until_ms > 0);
  }
  ApplyLocked(event);
  if (write) QueueLocked(std::move(event));
}

int64_t LoginGuard::Clear(const std::optional<std::string>& user_name,
                          const std::optional<std::string>& client_address, const std::string& by) {
  authz::LoginEventRecord event{NewEventId(), 0, "clear", user_name ? UserKey(*user_name) : std::string(),
                                user_name ? DisplayName(*user_name) : std::string(),
                                client_address ? AddressKey(*client_address) : std::string(), origin_};
  std::vector<std::pair<std::string, std::string>> ended;
  int64_t cleared = 0;
  {
    std::lock_guard lock(mutex_);
    event.at_ms = Now();
    cleared = ClearLocked(event.user_key, event.address, event.at_ms, &ended);
    QueueLocked(event);
  }
  LogFields fields{{"kind", "authentication"},
                   {"status", "cleared"},
                   {"by", by},
                   {"user", user_name ? event.user_name : "(every user)"},
                   {"client_address", client_address ? event.address : "(every address)"},
                   {"cleared", std::to_string(cleared)},
                   {"locks_ended", std::to_string(ended.size())}};
  Log(LogLevel::kWarning, "security: an administrator cleared failed sign-in counts and locks", fields);
  Audit(LogLevel::kWarning, "sign-in locks cleared", std::move(fields));
  return cleared;
}

void LoginGuard::PruneLocked(int64_t now) {
  const int64_t window = settings_.failure_window_seconds * kSecond;
  const int64_t keep_success = std::max(kSuccessRetention, settings_.known_address_days * kDay);
  auto idle = [&](const Counter& counter) { return counter.failures == 0 || now - counter.last_failure_ms > window; };
  for (auto it = pairs_.begin(); it != pairs_.end();) {
    const Pair& pair = it->second;
    const bool old_success = pair.last_success_ms == 0 || now - pair.last_success_ms > keep_success;
    if (pair.locked_until_ms <= now && pair.in_flight == 0 && idle(pair.counter) && old_success) {
      it = pairs_.erase(it);
    } else {
      ++it;
    }
  }
  for (auto it = users_.begin(); it != users_.end();) {
    const bool old_success = it->second.last_success_ms == 0 || now - it->second.last_success_ms > keep_success;
    it = idle(it->second.counter) && old_success ? users_.erase(it) : std::next(it);
  }
  for (auto it = addresses_.begin(); it != addresses_.end();) it = idle(it->second) ? addresses_.erase(it) : std::next(it);
  // Still over the bound (many names or addresses at once): forget the least recent first,
  // never a locked pair.
  auto activity = [](const auto& entry) { return entry.last_failure_ms; };
  if (pairs_.size() > settings_.max_entries) {
    std::vector<std::pair<int64_t, PairKey>> order;
    for (const auto& [key, pair] : pairs_) {
      if (pair.locked_until_ms <= now) order.emplace_back(std::max(pair.counter.last_failure_ms, pair.last_success_ms), key);
    }
    std::sort(order.begin(), order.end());
    for (std::size_t i = 0; i < order.size() && pairs_.size() > settings_.max_entries; ++i) pairs_.erase(order[i].second);
  }
  if (users_.size() > settings_.max_entries) {
    std::vector<std::pair<int64_t, std::string>> order;
    for (const auto& [key, user] : users_) order.emplace_back(std::max(user.counter.last_failure_ms, user.last_success_ms), key);
    std::sort(order.begin(), order.end());
    for (std::size_t i = 0; i < order.size() && users_.size() > settings_.max_entries; ++i) users_.erase(order[i].second);
  }
  if (addresses_.size() > settings_.max_entries) {
    std::vector<std::pair<int64_t, std::string>> order;
    for (const auto& [key, counter] : addresses_) order.emplace_back(activity(counter), key);
    std::sort(order.begin(), order.end());
    for (std::size_t i = 0; i < order.size() && addresses_.size() > settings_.max_entries; ++i) {
      addresses_.erase(order[i].second);
    }
  }
}

std::string LoginGuard::Load() {
  if (!store_) return {};
  std::vector<authz::LoginEventRecord> events;
  if (std::string error = store_->Since(0, events); !error.empty()) return error;
  std::lock_guard lock(mutex_);
  for (const auto& e : events) {
    if (seen_.count(e.id)) continue;
    seen_[e.id] = e.at_ms;
    ApplyLocked(e);
    read_up_to_ms_ = std::max(read_up_to_ms_, e.at_ms);
  }
  return {};
}

void LoginGuard::Sync() {
  std::vector<authz::LoginEventRecord> batch;
  {
    std::lock_guard lock(mutex_);
    batch.swap(pending_);
  }
  std::string problem;
  if (store_ && !batch.empty()) {
    problem = store_->Append(batch);
    if (!problem.empty()) {
      std::lock_guard lock(mutex_);
      batch.insert(batch.end(), pending_.begin(), pending_.end());
      if (batch.size() > kMaxPending) batch.erase(batch.begin(), batch.end() - kMaxPending);
      pending_.swap(batch);
    }
  }
  std::vector<authz::LoginEventRecord> remote;
  if (store_ && problem.empty()) {
    int64_t since = 0;
    {
      std::lock_guard lock(mutex_);
      since = std::max<int64_t>(0, read_up_to_ms_ - kReadOverlap);
    }
    problem = store_->Since(since, remote);
  }
  std::vector<std::pair<std::string, std::string>> ended;
  bool prune_store = false;
  bool report_problem = false;
  bool report_recovery = false;
  {
    std::lock_guard lock(mutex_);
    for (const auto& e : remote) {
      read_up_to_ms_ = std::max(read_up_to_ms_, e.at_ms);
      if (e.origin == origin_ || seen_.count(e.id)) continue;
      seen_[e.id] = e.at_ms;
      ApplyLocked(e);
    }
    for (auto it = seen_.begin(); it != seen_.end();) {
      it = it->second < read_up_to_ms_ - 4 * kReadOverlap ? seen_.erase(it) : std::next(it);
    }
    const int64_t now = Now();
    for (auto& [key, pair] : pairs_) {
      if (pair.locked_until_ms == 0 || pair.locked_until_ms > now) continue;
      if (pair.locked_here) ended.emplace_back(pair.user_name, key.second);
      pair.counter = {};
      pair.locked_until_ms = 0;
      pair.locked_here = false;
    }
    if (now - last_prune_ms_ > kMemoryPruneEvery) {
      PruneLocked(now);
      last_prune_ms_ = now;
    }
    if (store_ && now - last_store_prune_ms_ > kStorePruneEvery) {
      prune_store = true;
      last_store_prune_ms_ = now;
    }
    last_problem_ = problem;
    report_problem = !problem.empty() && !store_failing_;
    report_recovery = problem.empty() && store_failing_;
    store_failing_ = !problem.empty();
  }
  for (const auto& [user, address] : ended) {
    LogFields fields{{"kind", "authentication"}, {"status", "unlocked"}, {"reason", "expired"},
                     {"user", user}, {"client_address", address}};
    Log(LogLevel::kWarning, "security: a sign-in lock ended", fields);
    Audit(LogLevel::kWarning, "sign-in lock ended", std::move(fields));
  }
  if (report_problem) {
    Log(LogLevel::kWarning,
        "login guard: the access control store does not take or give sign-in records; this server counts failed "
        "sign-ins on its own until it does",
        {{"error", problem}});
  } else if (report_recovery) {
    Log(LogLevel::kInfo, "login guard: the access control store takes sign-in records again");
  }
  if (prune_store) {
    const int64_t now = Now();
    const int64_t keep_failures =
        (std::max(settings_.failure_window_seconds, settings_.lockout_seconds) + 3600) * kSecond;
    const int64_t keep_success = std::max(kSuccessRetention, settings_.known_address_days * kDay);
    if (std::string error = store_->Prune(now - keep_failures, now - keep_success); !error.empty()) {
      Log(LogLevel::kDebug, "login guard: pruning old sign-in records failed", {{"error", error}});
    }
  }
}

void LoginGuard::Loop(std::chrono::milliseconds interval) {
  std::unique_lock lock(loop_mutex_);
  while (!stopping_) {
    lock.unlock();
    try {
      Sync();
    } catch (const std::exception& e) {
      Log(LogLevel::kWarning, "login guard: a sync round failed", {{"error", e.what()}});
    }
    lock.lock();
    loop_cv_.wait_for(lock, interval, [this] { return stopping_; });
  }
}

void LoginGuard::Start(std::chrono::milliseconds interval) {
  std::lock_guard lock(loop_mutex_);
  if (thread_.joinable()) return;
  stopping_ = false;
  thread_ = std::thread([this, interval] { Loop(interval); });
}

void LoginGuard::Stop() {
  {
    std::lock_guard lock(loop_mutex_);
    stopping_ = true;
  }
  loop_cv_.notify_all();
  if (thread_.joinable()) thread_.join();
  // What this instance recorded since the last round still reaches the store.
  if (store_) {
    std::vector<authz::LoginEventRecord> batch;
    {
      std::lock_guard lock(mutex_);
      batch.swap(pending_);
    }
    if (!batch.empty()) (void)store_->Append(batch);
  }
}

std::string LoginGuard::PendingProblem() {
  std::lock_guard lock(mutex_);
  return pending_.empty() ? std::string() : last_problem_;
}

std::vector<LoginGuard::Entry> LoginGuard::List() {
  std::lock_guard lock(mutex_);
  const int64_t now = Now();
  std::vector<Entry> out;
  for (const auto& [key, pair] : pairs_) {
    const bool locked = pair.locked_until_ms > now;
    const int64_t failures = locked || pair.locked_until_ms == 0 ? Current(pair.counter, now) : 0;
    if (!locked && failures == 0) continue;
    out.push_back({"pair", pair.user_name, key.second, locked ? pair.counter.failures : failures,
                   pair.counter.last_failure_ms, locked ? pair.locked_until_ms : 0,
                   locked ? pair.locked_until_ms : 0});
  }
  for (const auto& [key, user] : users_) {
    const int64_t failures = Current(user.counter, now);
    if (failures == 0) continue;
    const int64_t until = BackoffUntil(user.counter, settings_.user_backoff_after, now);
    out.push_back({"user", user.user_name, "", failures, user.counter.last_failure_ms, 0, until > now ? until : 0});
  }
  for (const auto& [key, counter] : addresses_) {
    const int64_t failures = Current(counter, now);
    if (failures == 0) continue;
    const int64_t until = BackoffUntil(counter, settings_.address_backoff_after, now);
    out.push_back({"address", "", key, failures, counter.last_failure_ms, 0, until > now ? until : 0});
  }
  return out;
}

LoginGuard::UserSummary LoginGuard::ForUser(const std::string& user_name) {
  std::lock_guard lock(mutex_);
  UserSummary out;
  auto it = users_.find(UserKey(user_name));
  if (it == users_.end()) return out;
  const int64_t now = Now();
  if (it->second.last_success_ms > 0) out.last_sign_in_ms = it->second.last_success_ms;
  out.failures = Current(it->second.counter, now);
  if (out.failures > 0) out.last_failure_ms = it->second.counter.last_failure_ms;
  return out;
}

LoginGuard::Counts LoginGuard::counts() {
  std::lock_guard lock(mutex_);
  Counts out = counts_;
  const int64_t now = Now();
  out.active_locks = 0;
  for (const auto& [key, pair] : pairs_) out.active_locks += pair.locked_until_ms > now ? 1 : 0;
  return out;
}

// ---- The access-control store's records ----

namespace {

class AccessControlEvents : public LoginEventStore {
 public:
  explicit AccessControlEvents(std::shared_ptr<authz::AccessControl> access) : access_(std::move(access)) {}

  std::string Append(const std::vector<authz::LoginEventRecord>& events) override {
    const auto status = access_->AppendLoginEvents(events);
    return status.ok() ? std::string() : status.message();
  }
  std::string Since(int64_t since_ms, std::vector<authz::LoginEventRecord>& out) override {
    auto result = access_->LoginEventsSince(since_ms);
    if (!result.ok()) return result.status().message();
    out = std::move(*result);
    return {};
  }
  std::string Prune(int64_t failures_before_ms, int64_t successes_before_ms) override {
    const auto status = access_->PruneLoginEvents(failures_before_ms, successes_before_ms);
    return status.ok() ? std::string() : status.message();
  }

 private:
  std::shared_ptr<authz::AccessControl> access_;
};

}  // namespace

std::shared_ptr<LoginEventStore> StoreLoginEvents(std::shared_ptr<authz::AccessControl> access) {
  if (!access) return nullptr;
  return std::make_shared<AccessControlEvents>(std::move(access));
}

LoginGuardSettings LoginGuardSettingsFrom(const Config& config) {
  LoginGuardSettings settings;
  settings.lockout_threshold = config.login_lockout_threshold;
  settings.lockout_seconds = config.login_lockout_seconds;
  settings.failure_window_seconds = config.login_failure_window_seconds;
  settings.user_backoff_after = config.login_user_backoff_after;
  settings.address_backoff_after = config.login_address_backoff_after;
  settings.backoff_base_seconds = config.login_backoff_base_seconds;
  settings.backoff_max_seconds = config.login_backoff_max_seconds;
  settings.known_address_days = config.login_known_address_days;
  return settings;
}

}  // namespace hardcharger
