// The login guard's rules (login_guard.hpp) with the clock in the test's hands: a pair's
// lock and its end, the per-user and per-address delays, the known-address exemption, an
// administrator's clear, the records replicas share, and the keys. Also the store's sign-in
// records (in-memory and DuckDB-file stores) and the password policy.
#include <gtest/gtest.h>

#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "authz/password.hpp"
#include "authz/store.hpp"
#include "login_guard.hpp"

using hardcharger::LoginEventStore;
using hardcharger::LoginGuard;
using hardcharger::LoginGuardSettings;
using hardcharger::authz::LoginEventRecord;

namespace {

constexpr int64_t kStart = 1'800'000'000'000;  // 2027-01-15, ms

// A store shared by several guards (replicas), in memory.
class SharedEvents : public LoginEventStore {
 public:
  std::string Append(const std::vector<LoginEventRecord>& events) override {
    std::lock_guard lock(mutex_);
    if (failing) return "store unavailable";
    rows.insert(rows.end(), events.begin(), events.end());
    return {};
  }
  std::string Since(int64_t since_ms, std::vector<LoginEventRecord>& out) override {
    std::lock_guard lock(mutex_);
    if (failing) return "store unavailable";
    for (const auto& e : rows) {
      if (e.at_ms >= since_ms) out.push_back(e);
    }
    return {};
  }
  std::string Prune(int64_t, int64_t) override { return {}; }

  std::vector<LoginEventRecord> rows;
  bool failing = false;

 private:
  std::mutex mutex_;
};

struct Fixture {
  int64_t now = kStart;
  LoginGuardSettings settings;
  std::shared_ptr<SharedEvents> events = std::make_shared<SharedEvents>();

  std::unique_ptr<LoginGuard> Guard(const std::string& origin = "a", bool with_store = true) {
    return std::make_unique<LoginGuard>(settings, origin, with_store ? events : nullptr, [this] { return now; });
  }
  void Advance(int64_t seconds) { now += seconds * 1000; }
};

void Fail(LoginGuard& guard, const std::string& user, const std::string& address, int times = 1) {
  for (int i = 0; i < times; ++i) guard.RecordFailure(user, address);
}

}  // namespace

TEST(LoginGuard, APairLocksAfterTheThresholdAndOnlyThatPair) {
  Fixture f;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 4);
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
  Fail(*guard, "alice", "203.0.113.9");
  const auto refused = guard->Check("alice", "203.0.113.9");
  EXPECT_FALSE(refused.allowed);
  EXPECT_EQ(refused.reason, "locked");
  EXPECT_EQ(refused.retry_after_seconds, 900);
  // The same user from another address: still checked (no lock from everywhere). The user's
  // own delay starts at its 5th failure (1 s), so a second later it is accepted.
  f.Advance(1);
  EXPECT_TRUE(guard->Check("alice", "198.51.100.7").allowed);
  // Another user from the locked address is not locked either.
  EXPECT_TRUE(guard->Check("bob", "203.0.113.9").allowed);
  EXPECT_EQ(guard->counts().active_locks, 1);
  EXPECT_EQ(guard->counts().locks, 1);
  EXPECT_EQ(guard->counts().failures, 5);
  EXPECT_EQ(guard->counts().throttled_locked, 1);
}

TEST(LoginGuard, ALockEndsAfterItsDurationAndCountsAfresh) {
  Fixture f;
  f.settings.user_backoff_after = 0;  // the user's own delay is another test's
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 5);
  f.Advance(899);
  EXPECT_EQ(guard->Check("alice", "203.0.113.9").retry_after_seconds, 1);
  f.Advance(1);
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
  guard->Sync();  // ends the lock (logged)
  EXPECT_EQ(guard->counts().active_locks, 0);
  // One more failure is the first of a new count, not a sixth.
  Fail(*guard, "alice", "203.0.113.9");
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
}

TEST(LoginGuard, ASuccessResetsItsPair) {
  Fixture f;
  f.settings.user_backoff_after = 0;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 4);
  guard->RecordSuccess("alice", "203.0.113.9");
  Fail(*guard, "alice", "203.0.113.9", 4);
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
  Fail(*guard, "alice", "203.0.113.9");
  EXPECT_FALSE(guard->Check("alice", "203.0.113.9").allowed);
}

TEST(LoginGuard, AnUnknownNameIsTreatedLikeAKnownOne) {
  // The guard never asks whether a user exists: the same failures give the same answer.
  Fixture f;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 5);
  Fail(*guard, "no-such-user", "203.0.113.10", 5);
  const auto known = guard->Check("alice", "203.0.113.9");
  const auto unknown = guard->Check("no-such-user", "203.0.113.10");
  EXPECT_EQ(known.allowed, unknown.allowed);
  EXPECT_EQ(known.reason, unknown.reason);
  EXPECT_EQ(known.retry_after_seconds, unknown.retry_after_seconds);
}

TEST(LoginGuard, TheUserDelayGrowsIsCappedAndNeverLocks) {
  Fixture f;
  f.settings.lockout_threshold = 0;
  f.settings.address_backoff_after = 0;
  auto guard = f.Guard();
  // Every failure from a new address: only the per-user counter adds up.
  int address = 1;
  auto fail_once = [&] { Fail(*guard, "admin", "198.51.100." + std::to_string(address++)); };
  for (int i = 0; i < 4; ++i) fail_once();
  EXPECT_TRUE(guard->Check("admin", "192.0.2.1").allowed);
  const int64_t expected[] = {1, 2, 4, 8, 16, 30, 30, 30};
  for (const int64_t seconds : expected) {
    fail_once();
    const auto refused = guard->Check("admin", "192.0.2.1");
    EXPECT_FALSE(refused.allowed);
    EXPECT_EQ(refused.reason, "user_backoff");
    EXPECT_EQ(refused.retry_after_seconds, seconds);
  }
  // Once the delay has passed, any address may try: a delay, never a lock.
  f.Advance(30);
  EXPECT_TRUE(guard->Check("admin", "192.0.2.1").allowed);
}

TEST(LoginGuard, AnAddressTheUserSignedInFromSkipsTheUserDelay) {
  Fixture f;
  f.settings.lockout_threshold = 0;
  auto guard = f.Guard();
  guard->RecordSuccess("admin", "192.0.2.1");
  f.Advance(60);
  for (int i = 1; i <= 12; ++i) Fail(*guard, "admin", "198.51.100." + std::to_string(i));
  EXPECT_FALSE(guard->Check("admin", "192.0.2.2").allowed);
  EXPECT_TRUE(guard->Check("admin", "192.0.2.1").allowed);
  // ... for login_known_address_days only.
  f.Advance(31 * 86400);
  for (int i = 1; i <= 12; ++i) Fail(*guard, "admin", "198.51.100." + std::to_string(i));
  EXPECT_FALSE(guard->Check("admin", "192.0.2.1").allowed);
}

TEST(LoginGuard, AKnownAddressStillLocks) {
  Fixture f;
  auto guard = f.Guard();
  guard->RecordSuccess("alice", "203.0.113.9");
  Fail(*guard, "alice", "203.0.113.9", 5);
  EXPECT_EQ(guard->Check("alice", "203.0.113.9").reason, "locked");
}

TEST(LoginGuard, OneAddressSprayingManyUsersIsSlowed) {
  Fixture f;
  auto guard = f.Guard();
  for (int i = 0; i < 19; ++i) Fail(*guard, "user" + std::to_string(i), "203.0.113.9");
  EXPECT_TRUE(guard->Check("someone", "203.0.113.9").allowed);
  Fail(*guard, "user19", "203.0.113.9");
  const auto refused = guard->Check("someone", "203.0.113.9");
  EXPECT_FALSE(refused.allowed);
  EXPECT_EQ(refused.reason, "address_backoff");
  EXPECT_EQ(refused.retry_after_seconds, 1);
  EXPECT_TRUE(guard->Check("someone", "203.0.113.10").allowed);  // another address
}

TEST(LoginGuard, CountsForgetAfterTheWindow) {
  Fixture f;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 4);
  f.Advance(901);
  Fail(*guard, "alice", "203.0.113.9", 4);
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
}

TEST(LoginGuard, AnAdministratorClearsAPairAUserAnAddressOrEverything) {
  Fixture f;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 5);
  Fail(*guard, "bob", "203.0.113.10", 5);
  EXPECT_EQ(guard->Clear(std::string("alice"), std::string("203.0.113.9"), "admin"), 2);  // pair and user
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
  EXPECT_FALSE(guard->Check("bob", "203.0.113.10").allowed);  // the other pair stands
  EXPECT_GT(guard->Clear(std::string("BOB"), std::nullopt, "admin"), 0);  // names fold case
  EXPECT_TRUE(guard->Check("bob", "203.0.113.10").allowed);
  Fail(*guard, "carol", "203.0.113.11", 5);
  EXPECT_GT(guard->Clear(std::nullopt, std::nullopt, "admin"), 0);
  EXPECT_TRUE(guard->Check("carol", "203.0.113.11").allowed);
  EXPECT_TRUE(guard->List().empty());
}

TEST(LoginGuard, TheListingShowsPairsUsersAndAddresses) {
  Fixture f;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 5);
  const auto entries = guard->List();
  bool pair = false, user = false, address = false;
  for (const auto& e : entries) {
    if (e.scope == "pair") {
      pair = true;
      EXPECT_EQ(e.user_name, "alice");
      EXPECT_EQ(e.client_address, "203.0.113.9");
      EXPECT_EQ(e.failures, 5);
      EXPECT_EQ(e.locked_until_ms, kStart + 900'000);
    }
    user = user || (e.scope == "user" && e.failures == 5);
    address = address || (e.scope == "address" && e.client_address == "203.0.113.9");
  }
  EXPECT_TRUE(pair && user && address);
  EXPECT_EQ(guard->ForUser("ALICE").failures, 5);
  EXPECT_FALSE(guard->ForUser("alice").last_sign_in_ms.has_value());
  guard->RecordSuccess("alice", "198.51.100.1");
  EXPECT_EQ(guard->ForUser("alice").last_sign_in_ms, kStart);
}

TEST(LoginGuard, ReplicasReachTheSameCountsThroughTheStore) {
  Fixture f;
  auto a = f.Guard("replica-a");
  auto b = f.Guard("replica-b");
  Fail(*a, "alice", "203.0.113.9", 3);
  Fail(*b, "alice", "203.0.113.9", 1);
  a->Sync();
  b->Sync();
  a->Sync();
  // Neither has seen five yet: the fifth failure, on either, locks the pair on both.
  EXPECT_TRUE(a->Check("alice", "203.0.113.9").allowed);
  Fail(*b, "alice", "203.0.113.9");
  EXPECT_FALSE(b->Check("alice", "203.0.113.9").allowed);
  b->Sync();
  a->Sync();
  EXPECT_EQ(a->Check("alice", "203.0.113.9").reason, "locked");
  // A clear on one reaches the other.
  b->Clear(std::string("alice"), std::nullopt, "admin");
  b->Sync();
  a->Sync();
  EXPECT_TRUE(a->Check("alice", "203.0.113.9").allowed);
  // A new process (or --verify-credentials) loads everything at once.
  Fail(*a, "dave", "203.0.113.12", 5);
  a->Sync();
  auto fresh = f.Guard("verifier");
  EXPECT_EQ(fresh->Load(), "");
  EXPECT_EQ(fresh->Check("dave", "203.0.113.12").reason, "locked");
}

TEST(LoginGuard, AStoreThatFailsKeepsTheRecordsForLater) {
  Fixture f;
  auto a = f.Guard("replica-a");
  f.events->failing = true;
  Fail(*a, "alice", "203.0.113.9", 5);
  a->Sync();
  EXPECT_NE(a->PendingProblem(), "");
  EXPECT_FALSE(a->Check("alice", "203.0.113.9").allowed);  // its own counts still hold
  f.events->failing = false;
  a->Sync();
  EXPECT_EQ(a->PendingProblem(), "");
  EXPECT_EQ(f.events->rows.size(), 5u);
}

TEST(LoginGuard, SuccessesAreWrittenSparingly) {
  Fixture f;
  auto a = f.Guard();
  for (int i = 0; i < 10; ++i) a->RecordSuccess("alice", "203.0.113.9");
  a->Sync();
  EXPECT_EQ(f.events->rows.size(), 1u);
  Fail(*a, "alice", "203.0.113.9");
  a->RecordSuccess("alice", "203.0.113.9");  // ends a failure count: written at once
  a->Sync();
  EXPECT_EQ(f.events->rows.size(), 3u);
}

TEST(LoginGuard, MemoryStaysBounded) {
  Fixture f;
  f.settings.max_entries = 100;
  auto guard = f.Guard("a", false);
  for (int i = 0; i < 1000; ++i) Fail(*guard, "name" + std::to_string(i), "10.0." + std::to_string(i / 250) + "." + std::to_string(i % 250));
  EXPECT_LE(guard->List().size(), 3u * 101u);
}

TEST(LoginGuard, Keys) {
  EXPECT_EQ(LoginGuard::UserKey("Alice"), "alice");
  EXPECT_EQ(LoginGuard::UserKey(std::string(300, 'x')).rfind("sha256:", 0), 0u);
  EXPECT_EQ(LoginGuard::AddressKey("127.0.0.1:51234"), "127.0.0.1");
  EXPECT_EQ(LoginGuard::AddressKey("203.0.113.9"), "203.0.113.9");
  EXPECT_EQ(LoginGuard::AddressKey("[2001:db8:1:2:3:4:5:6]:443"), "2001:db8:1:2::/64");
  EXPECT_EQ(LoginGuard::AddressKey("2001:db8:1:2:ffff::1"), "2001:db8:1:2::/64");
  EXPECT_EQ(LoginGuard::AddressKey("[::ffff:10.1.2.3]:80"), "10.1.2.3");
  EXPECT_EQ(LoginGuard::AddressKey(""), "");
}

TEST(LoginGuard, AnIpv6HostCannotDodgeItsLockByChangingItsInterfaceId) {
  Fixture f;
  auto guard = f.Guard();
  for (int i = 1; i <= 5; ++i) Fail(*guard, "alice", "2001:db8:1:2::" + std::to_string(i));
  EXPECT_EQ(guard->Check("alice", "2001:db8:1:2::99").reason, "locked");
  f.Advance(1);
  EXPECT_TRUE(guard->Check("alice", "2001:db8:1:3::1").allowed);  // another /64
}

TEST(LoginGuard, SettingsOfZeroTurnRulesOff) {
  Fixture f;
  f.settings.lockout_threshold = 0;
  f.settings.backoff_max_seconds = 0;
  auto guard = f.Guard();
  Fail(*guard, "alice", "203.0.113.9", 50);
  EXPECT_TRUE(guard->Check("alice", "203.0.113.9").allowed);
}

// ---- The store's records ----

namespace {

using hardcharger::authz::RbacStore;
using hardcharger::authz::StoreOptions;

void ExerciseStore(RbacStore& store) {
  ASSERT_TRUE(store.AppendLoginEvents({{"e1", 100, "failure", "alice", "alice", "203.0.113.9", "a"},
                                       {"e2", 200, "success", "alice", "alice", "203.0.113.9", "a"},
                                       {"e3", 300, "success", "alice", "alice", "203.0.113.9", "b"},
                                       {"e4", 400, "clear", "", "", "", "b"}})
                  .ok());
  auto all = store.LoginEventsSince(0);
  ASSERT_TRUE(all.ok()) << all.status().ToString();
  ASSERT_EQ(all->size(), 4u);
  EXPECT_EQ((*all)[0].id, "e1");
  EXPECT_EQ((*all)[0].address, "203.0.113.9");
  EXPECT_EQ((*all)[3].kind, "clear");
  auto later = store.LoginEventsSince(250);
  ASSERT_TRUE(later.ok());
  EXPECT_EQ(later->size(), 2u);
  // Failures and clears before 350 go, and the older of two successes of one pair.
  ASSERT_TRUE(store.PruneLoginEvents(350, 0).ok());
  auto kept = store.LoginEventsSince(0);
  ASSERT_TRUE(kept.ok());
  ASSERT_EQ(kept->size(), 2u);
  EXPECT_EQ((*kept)[0].id, "e3");
  EXPECT_EQ((*kept)[1].id, "e4");
}

std::filesystem::path PrivateDir() {
  auto dir = std::filesystem::temp_directory_path() /
             ("hc-login-test-" + hardcharger::authz::StoreTransaction::NewId());
  std::filesystem::create_directory(dir);
  std::filesystem::permissions(dir, std::filesystem::perms::owner_all, std::filesystem::perm_options::replace);
  return dir;
}

}  // namespace

TEST(LoginEventStore, InMemory) {
  auto store = RbacStore::Open("memory");
  ASSERT_TRUE(store.ok()) << store.status().ToString();
  ExerciseStore(**store);
}

TEST(LoginEventStore, DuckDBFileAndItsReadOnlyOpenings) {
  const auto dir = PrivateDir();
  const std::string uri = "duckdb:" + (dir / "rbac.duckdb").string();
  {
    auto store = RbacStore::Open(uri);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    ExerciseStore(**store);
  }
  {
    // Read-only: reads, and refuses to write.
    StoreOptions options;
    options.uri = uri;
    options.read_only = true;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    EXPECT_EQ((*store)->LoginEventsSince(0)->size(), 2u);
    const auto refused = (*store)->AppendLoginEvents({{"e5", 500, "failure", "bob", "bob", "", "v"}});
    EXPECT_FALSE(refused.ok());
    EXPECT_NE(refused.message().find("opened read-only"), std::string::npos) << refused.ToString();
  }
  {
    // Read-only recording sign-ins (--verify-credentials): its records, nothing else.
    StoreOptions options;
    options.uri = uri;
    options.read_only = true;
    options.record_sign_ins = true;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    ASSERT_TRUE((*store)->AppendLoginEvents({{"e5", 500, "failure", "bob", "bob", "", "v"}}).ok());
    EXPECT_EQ((*store)->LoginEventsSince(0)->size(), 3u);
    const auto other = (*store)->Mutate([](auto&, auto&) { return arrow::Status::OK(); });
    EXPECT_FALSE(other.ok());
    EXPECT_NE(other.message().find("opened read-only"), std::string::npos) << other.ToString();
  }
  std::filesystem::remove_all(dir);
}

// ---- The password policy ----

TEST(PasswordPolicy, LengthCountsCharactersAndClassesAreOptional) {
  using hardcharger::authz::CheckPasswordPolicy;
  using hardcharger::authz::PasswordPolicy;
  const PasswordPolicy defaults;
  EXPECT_FALSE(CheckPasswordPolicy("elevenchars", defaults).ok());
  EXPECT_TRUE(CheckPasswordPolicy("twelve chars", defaults).ok());
  // Twelve characters, more bytes: "é" is two bytes.
  EXPECT_FALSE(CheckPasswordPolicy("ééééééééééé", defaults).ok());  // 11 characters, 22 bytes
  EXPECT_TRUE(CheckPasswordPolicy("éééééééééééé", defaults).ok());
  const auto short_message = CheckPasswordPolicy("short", defaults).message();
  EXPECT_NE(short_message.find("at least 12 characters"), std::string::npos) << short_message;
  EXPECT_EQ(CheckPasswordPolicy("s3cr3t-unique-pw", defaults).message().find("s3cr3t"), std::string::npos);
  PasswordPolicy classes{12, 3, 0};
  EXPECT_FALSE(CheckPasswordPolicy("alllowercaseletters", classes).ok());
  EXPECT_TRUE(CheckPasswordPolicy("lowercase and 123", classes).ok());  // lower, other (space), digit
  EXPECT_TRUE(CheckPasswordPolicy("Lowercase4567", classes).ok());
}
