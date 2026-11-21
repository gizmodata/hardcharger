// The access-control store's own rules that need no PostgreSQL: the bounds added to a
// PostgreSQL connection string, error redaction, the cluster a store belongs to and the
// schema version it refuses (on DuckDB-file and in-memory stores), an owner change applied
// in memory, and callers served in turn. The outage behaviour
// against a real PostgreSQL is covered by server/tests/test_rbac_store_outage.py.
#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <future>
#include <mutex>
#include <filesystem>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "authz/store.hpp"
#include "authz/store_link.hpp"
#include "logging.hpp"
#include "redact.hpp"

using hardcharger::RedactCredentials;
using hardcharger::authz::ObjectKey;
using hardcharger::authz::ObjectPath;
using hardcharger::authz::OwnerKind;
using hardcharger::authz::OwnerRecord;
using hardcharger::authz::RbacStore;
using hardcharger::authz::Snapshot;
using hardcharger::authz::StoreTransaction;
using hardcharger::authz::StoreConnectionWithTimeouts;
using hardcharger::authz::StoreOptions;

namespace {

bool Has(const std::string& text, const std::string& part) { return text.find(part) != std::string::npos; }

std::filesystem::path PrivateDir() {
  auto dir = std::filesystem::temp_directory_path() /
             ("hc-store-test-" + hardcharger::authz::StoreTransaction::NewId());
  std::filesystem::create_directory(dir);
  std::filesystem::permissions(dir, std::filesystem::perms::owner_all, std::filesystem::perm_options::replace);
  return dir;
}

OwnerRecord Owner(const std::string& table, const std::string& user) {
  OwnerRecord owner;
  owner.object = ObjectPath{"db", {"main"}, table};
  owner.owner_kind = OwnerKind::kUser;
  owner.owner_id = user;
  return owner;
}

// Every owner record, comparable.
std::set<std::string> Owners(const Snapshot& snapshot) {
  std::set<std::string> out;
  for (const auto& o : snapshot.owners()) {
    out.insert(ObjectKey(o.object) + "|" + std::to_string(static_cast<int>(o.type)) + "|" +
               std::to_string(static_cast<int>(o.owner_kind)) + "|" + o.owner_id + "|" + o.definition_hash);
  }
  return out;
}

}  // namespace

TEST(StoreConnection, UrlGetsEveryBoundItLacks) {
  const auto out = StoreConnectionWithTimeouts("postgresql://u:pw@db.example:5432/hc", std::chrono::seconds(10));
  EXPECT_EQ(out,
            "postgresql://u:pw@db.example:5432/hc?connect_timeout=10&keepalives=1&keepalives_idle=5&"
            "keepalives_interval=2&keepalives_count=3&tcp_user_timeout=10000&options=-c%20statement_timeout%3D10000");
}

TEST(StoreConnection, UrlKeepsWhatItSets) {
  const auto out = StoreConnectionWithTimeouts("postgres://u@h/db?sslmode=require&connect_timeout=30&options=-c%20x%3D1",
                                               std::chrono::seconds(4));
  EXPECT_TRUE(Has(out, "sslmode=require&connect_timeout=30&options=-c%20x%3D1&keepalives=1")) << out;
  EXPECT_FALSE(Has(out, "connect_timeout=4")) << out;
  EXPECT_FALSE(Has(out, "statement_timeout")) << out;
  EXPECT_TRUE(Has(out, "tcp_user_timeout=4000")) << out;
}

TEST(StoreConnection, QuestionMarkInThePasswordIsNotTheQuery) {
  const auto out = StoreConnectionWithTimeouts("postgresql://u:p?w@h/db", std::chrono::seconds(10));
  EXPECT_EQ(out.rfind("postgresql://u:p?w@h/db?connect_timeout=10&", 0), 0u) << out;
}

TEST(StoreConnection, KeywordFormGetsEveryBoundItLacks) {
  const auto out = StoreConnectionWithTimeouts("host=h dbname=db user=u password=pw keepalives_idle=60",
                                               std::chrono::seconds(10));
  EXPECT_EQ(out,
            "host=h dbname=db user=u password=pw keepalives_idle=60 connect_timeout=10 keepalives=1 "
            "keepalives_interval=2 keepalives_count=3 tcp_user_timeout=10000 options='-c statement_timeout=10000'");
}

TEST(StoreConnection, ConnectTimeoutIsAtLeastLibpqsMinimum) {
  const auto out = StoreConnectionWithTimeouts("host=h dbname=db", std::chrono::seconds(1));
  EXPECT_TRUE(Has(out, "connect_timeout=2 ")) << out;
}

TEST(StoreRedaction, KnownStringsAndEveryCredentialForm) {
  const std::string uri = "postgresql://u:s3cr@t?x@h/db";
  const std::string message = "Unable to connect to Postgres at \"" + uri + "?connect_timeout=10\": refused";
  const auto redacted = RedactCredentials(message, {uri + "?connect_timeout=10", uri});
  EXPECT_FALSE(Has(redacted, "s3cr")) << redacted;
  EXPECT_TRUE(Has(redacted, "<connection string>")) << redacted;
  EXPECT_TRUE(Has(redacted, "refused")) << redacted;
  for (const std::string text :
       {"postgresql://u:s3cr@t@h/db failed", "host=h password=s3cr3t dbname=d", "host=h password='s3 cr3t' dbname=d",
        "postgresql://u@h/db?sslmode=require&password=s3cr3t&x=1", "postgresql://u@h/db?sslpassword=s3cr3t",
        "'password': 's3cr3t'"}) {
    const auto out = RedactCredentials(text);
    EXPECT_FALSE(Has(out, "s3cr")) << text << " -> " << out;
    EXPECT_TRUE(Has(out, "***")) << text << " -> " << out;
  }
  EXPECT_EQ(RedactCredentials("connection to server at \"127.0.0.1\", port 5432 failed"),
            "connection to server at \"127.0.0.1\", port 5432 failed");
}

TEST(StoreOwnership, RecordsItsClusterAndRefusesAnother) {
  const auto dir = PrivateDir();
  const std::string uri = "duckdb:" + (dir / "rbac.duckdb").string();
  const std::string a = "11111111-1111-4111-8111-111111111111";
  const std::string b = "22222222-2222-4222-8222-222222222222";
  {
    StoreOptions options;
    options.uri = uri;
    options.cluster_id = a;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    EXPECT_EQ((*store)->cluster_id(), a);
  }
  {
    StoreOptions options;
    options.uri = uri;
    options.cluster_id = b;
    auto store = RbacStore::Open(options);
    ASSERT_FALSE(store.ok());
    EXPECT_TRUE(Has(store.status().message(), "the access control store belongs to cluster " + a)) << store.status();
    EXPECT_TRUE(Has(store.status().message(), "rbac_store_shared")) << store.status();
  }
  {
    StoreOptions options;  // the same cluster again (a replica, a restart)
    options.uri = uri;
    options.cluster_id = a;
    EXPECT_TRUE(RbacStore::Open(options).ok());
  }
  {
    StoreOptions options;  // sharing asked for
    options.uri = uri;
    options.cluster_id = b;
    options.shared = true;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    EXPECT_EQ((*store)->cluster_id(), a);  // the store still belongs to the first
  }
  {
    StoreOptions options;  // no cluster_id: can't tell, so it opens
    options.uri = uri;
    EXPECT_TRUE(RbacStore::Open(options).ok());
  }
  std::filesystem::remove_all(dir);
}

// Read-only (hardcharger-server --verify-credentials): a store without a schema is refused as
// not initialized (and none is created), an unclaimed store stays unclaimed, another cluster's
// store is still refused, and a write fails.
TEST(StoreReadOnly, NeverCreatesClaimsOrWrites) {
  const auto dir = PrivateDir();
  const std::string uri = "duckdb:" + (dir / "rbac.duckdb").string();
  const std::string a = "11111111-1111-4111-8111-111111111111";
  const std::string b = "22222222-2222-4222-8222-222222222222";
  {
    StoreOptions options;  // no file yet: not initialized, and still no file afterwards
    options.uri = uri;
    options.read_only = true;
    auto store = RbacStore::Open(options);
    ASSERT_FALSE(store.ok());
    EXPECT_TRUE(Has(store.status().message(), "has not been initialized")) << store.status();
    EXPECT_FALSE(std::filesystem::exists(dir / "rbac.duckdb"));
  }
  {
    StoreOptions options;  // "memory" has no schema either
    options.uri = "memory";
    options.read_only = true;
    auto store = RbacStore::Open(options);
    ASSERT_FALSE(store.ok());
    EXPECT_TRUE(Has(store.status().message(), "has not been initialized")) << store.status();
  }
  ASSERT_TRUE(RbacStore::Open(uri).ok());  // a server creates the schema; no cluster_id recorded
  {
    StoreOptions options;  // a read-only opening under a cluster id does not claim the store
    options.uri = uri;
    options.cluster_id = a;
    options.read_only = true;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    EXPECT_TRUE((*store)->read_only());
    EXPECT_EQ((*store)->cluster_id(), "");
    auto written = (*store)->Mutate([](StoreTransaction&, const Snapshot&) { return arrow::Status::OK(); });
    ASSERT_FALSE(written.ok());
    EXPECT_TRUE(Has(written.message(), "opened read-only")) << written;
    EXPECT_TRUE((*store)->Load().ok());
  }
  {
    StoreOptions options;  // still nobody's: a server with a cluster id takes it
    options.uri = uri;
    options.cluster_id = a;
    auto store = RbacStore::Open(options);
    ASSERT_TRUE(store.ok()) << store.status().ToString();
    EXPECT_EQ((*store)->cluster_id(), a);
  }
  {
    StoreOptions options;  // another cluster's store is refused read-only too
    options.uri = uri;
    options.cluster_id = b;
    options.read_only = true;
    auto store = RbacStore::Open(options);
    ASSERT_FALSE(store.ok());
    EXPECT_TRUE(Has(store.status().message(), "belongs to cluster " + a)) << store.status();
  }
  std::filesystem::remove_all(dir);
}

TEST(StoreOwnership, AStoreWithoutAClusterTakesTheFirstOne) {
  const auto dir = PrivateDir();
  const std::string uri = "duckdb:" + (dir / "rbac.duckdb").string();
  ASSERT_TRUE(RbacStore::Open(uri).ok());  // no cluster_id: none recorded
  StoreOptions options;
  options.uri = uri;
  options.cluster_id = "33333333-3333-4333-8333-333333333333";
  auto store = RbacStore::Open(options);
  ASSERT_TRUE(store.ok()) << store.status().ToString();
  EXPECT_EQ((*store)->cluster_id(), options.cluster_id);
  std::filesystem::remove_all(dir);
}

// An owner write applied to the snapshot in memory (RbacEngine::WriteOwner) is what reading
// the store again gives: the committed version is the one after, and the owners match after
// a new record, a replaced one and a removed one.
TEST(StoreWrite, AnOwnerChangeAppliedInMemoryMatchesAReload) {
  auto store = RbacStore::Open("memory");
  ASSERT_TRUE(store.ok()) << store.status().ToString();
  auto& s = **store;
  ASSERT_TRUE(s.Write([](StoreTransaction& txn) { return txn.UpsertOwner(Owner("kept", "u0")); }).ok());
  auto snapshot = s.Load();
  ASSERT_TRUE(snapshot.ok());
  std::shared_ptr<const Snapshot> mine = *snapshot;

  const OwnerRecord created = Owner("t", "u1");
  const OwnerRecord replaced = Owner("t", "u2");
  for (const OwnerRecord* change : {&created, &replaced, static_cast<const OwnerRecord*>(nullptr)}) {
    const ObjectPath object{"db", {"main"}, "t"};
    auto version = s.Write([&](StoreTransaction& txn) {
      return change ? txn.UpsertOwner(*change) : txn.DeleteOwner(object, hardcharger::authz::ObjectType::kTable);
    });
    ASSERT_TRUE(version.ok()) << version.status().ToString();
    EXPECT_EQ(*version, mine->version() + 1);
    mine = mine->WithOwner(*version, object, hardcharger::authz::ObjectType::kTable, change);
    auto reloaded = s.Load();
    ASSERT_TRUE(reloaded.ok());
    EXPECT_EQ((*reloaded)->version(), mine->version());
    EXPECT_EQ(Owners(**reloaded), Owners(*mine));
    // Everything else is carried over (the indexes are rebuilt).
    EXPECT_EQ((*reloaded)->roles().size(), mine->roles().size());
    EXPECT_NE(mine->FindRoleByName("PUBLIC"), nullptr);
  }
  EXPECT_EQ(Owners(*mine).size(), 1u);  // only "kept"
}

// Callers take turns at the store in the order they asked. A line far longer than the
// timeout is not an outage while no store statement hangs: every call succeeds, however long
// the callers ahead keep their turn with work of their own (2.1.2 took a mutex within the
// timeout, so a caller that waited longer failed with "access control store unavailable:
// timed out" although the store answered every statement at once).
TEST(StoreTurns, ALongLineAtAnAnsweringStoreIsNotAnOutage) {
  StoreOptions options;
  options.uri = "memory";
  options.timeout = std::chrono::milliseconds(200);
  auto store = RbacStore::Open(options);
  ASSERT_TRUE(store.ok()) << store.status().ToString();
  auto& s = **store;

  constexpr int kThreads = 16;
  constexpr int kCalls = 2;
  static constexpr auto kHold = std::chrono::milliseconds(50);  // the line: 16 x 2 x 50 ms = 8x the timeout
  std::atomic<int> failed{0};
  std::string first_failure;
  std::mutex failure_mutex;
  std::vector<std::thread> threads;
  for (int t = 0; t < kThreads; ++t) {
    threads.emplace_back([&s, &failed, &first_failure, &failure_mutex, t] {
      for (int i = 0; i < kCalls; ++i) {
        auto status = s.Mutate([&](StoreTransaction& txn, const Snapshot&) {
          std::this_thread::sleep_for(kHold);
          return txn.UpsertOwner(Owner("t" + std::to_string(t) + "_" + std::to_string(i), "u"));
        });
        if (!status.ok() && failed++ == 0) {
          std::lock_guard lock(failure_mutex);
          first_failure = status.ToString();
        }
      }
    });
  }
  for (auto& thread : threads) thread.join();
  EXPECT_EQ(failed.load(), 0) << first_failure;
  auto snapshot = s.Load();
  ASSERT_TRUE(snapshot.ok());
  EXPECT_EQ((*snapshot)->owners().size(), static_cast<size_t>(kThreads * kCalls));
}

// ---- The store's thread: a statement past its timeout --------------------------------------

namespace {

using hardcharger::authz::StoreLink;

constexpr std::string_view kGaveUp = "the access control store answered after its caller had given up";
constexpr std::string_view kTookTheTimeout = "the access control store took the whole rbac_store_timeout_seconds";

// The warnings the store's thread writes. `on_line` runs on the logging thread, for each line.
class StoreWarnings {
 public:
  explicit StoreWarnings(std::function<void(std::string_view)> on_line = {}) : on_line_(std::move(on_line)) {
    hardcharger::ConfigureLogging("info", "text");
    hardcharger::SetLogCaptureForTesting([this](hardcharger::LogLevel, std::string_view message) {
      if (on_line_) on_line_(message);
      std::lock_guard lock(mutex_);
      lines_.emplace_back(message);
      cv_.notify_all();
    });
  }
  ~StoreWarnings() { hardcharger::SetLogCaptureForTesting(nullptr); }
  // Waits (at most 30 s: only a failing test waits that long) for a line containing `text`.
  bool WaitFor(std::string_view text) {
    std::unique_lock lock(mutex_);
    return cv_.wait_for(lock, std::chrono::seconds(30), [&] {
      for (const auto& line : lines_) {
        if (line.find(text) != std::string::npos) return true;
      }
      return false;
    });
  }

 private:
  std::function<void(std::string_view)> on_line_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::vector<std::string> lines_;
};

std::unique_ptr<duckdb::DuckDB> MemoryDb() { return std::make_unique<duckdb::DuckDB>(nullptr); }

}  // namespace

// A statement the caller gave up on is logged when the store does answer, as slow, not down.
// The statement waits for its caller to have been told the store timed out: no clock decides.
TEST(StoreLinkSlowStatement, IsLoggedWhenItAnswersAfterItsCallerGaveUp) {
  StoreWarnings warnings;
  StoreLink link(MemoryDb(), std::chrono::milliseconds(100));
  std::promise<void> caller_told;
  auto told = caller_told.get_future().share();
  auto result = link.Call([told](duckdb::Connection& c) {
    told.wait();
    return c.Query("SELECT 42");
  }, "SELECT");
  ASSERT_FALSE(result.ok());
  EXPECT_NE(result.status().message().find("timed out after"), std::string::npos) << result.status().ToString();
  caller_told.set_value();
  EXPECT_TRUE(warnings.WaitFor(kGaveUp)) << "no warning that the store answered late";
}

// The warning never changes a caller's answer. The statement takes the whole timeout, and
// the store's thread keeps the link's lock past the caller's deadline before it hands the
// result over, so the caller waits for the lock and then finds its result (as it did before
// there was a warning). The warning then holds the logging thread until the caller has its
// answer: written before the handover (17340fc44), it gave the lock up to a caller that
// found no result and was told the store timed out.
TEST(StoreLinkSlowStatement, ACallerWaitingForTheLockGetsItsResultBeforeTheWarning) {
  constexpr auto kTimeout = std::chrono::milliseconds(100);
  std::mutex mutex;
  std::condition_variable cv;
  bool answered = false;
  StoreWarnings warnings([&](std::string_view line) {
    if (line.find("the access control store") == std::string_view::npos) return;
    std::unique_lock lock(mutex);
    cv.wait_for(lock, std::chrono::seconds(30), [&] { return answered; });
  });
  StoreLink link(MemoryDb(), kTimeout);
  std::atomic<std::chrono::steady_clock::time_point::rep> started{0};
  // The caller's deadline is its call's start plus the timeout, no later than the statement's
  // start plus the timeout: the lock is held until that has passed.
  link.SetBeforeHandoverForTesting([&] {
    const auto start = std::chrono::steady_clock::time_point(std::chrono::steady_clock::duration(started.load()));
    std::this_thread::sleep_until(start + kTimeout + std::chrono::milliseconds(50));
  });
  auto result = link.Call([&](duckdb::Connection& c) {
    started = std::chrono::steady_clock::now().time_since_epoch().count();
    return c.Query("SELECT 42");
  }, "SELECT");
  {
    std::lock_guard lock(mutex);
    answered = true;
  }
  cv.notify_all();
  ASSERT_TRUE(result.ok()) << result.status().ToString();
  EXPECT_EQ((*result)->Fetch()->GetValue(0, 0).GetValue<int32_t>(), 42);
  EXPECT_TRUE(warnings.WaitFor(kTookTheTimeout)) << "no warning that the statement took the whole timeout";
}
