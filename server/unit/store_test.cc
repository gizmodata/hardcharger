// The access-control store's own rules that need no PostgreSQL: the bounds added to a
// PostgreSQL connection string, error redaction, the cluster a store belongs to and the
// schema version it refuses (on DuckDB-file and in-memory stores). The outage behaviour
// against a real PostgreSQL is covered by server/tests/test_rbac_store_outage.py.
#include <gtest/gtest.h>

#include <chrono>
#include <filesystem>
#include <string>

#include "authz/store.hpp"
#include "redact.hpp"

using hardcharger::RedactCredentials;
using hardcharger::authz::RbacStore;
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
