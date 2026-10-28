// Storage locations (authz/locations.hpp): how paths are classified, compared and refused,
// and which grants cover them.
#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <random>
#include <string>

#include "authz/locations.hpp"

namespace fs = std::filesystem;
using namespace hardcharger::authz;

namespace {

StoragePath Parse(const std::string& path, bool pattern = true) {
  auto parsed = ParseStoragePath(path, {}, pattern);
  EXPECT_TRUE(parsed.ok()) << path << ": " << parsed.status().ToString();
  return parsed.ok() ? *parsed : StoragePath{};
}

std::string Refusal(const std::string& path) {
  auto parsed = ParseStoragePath(path, {}, true);
  EXPECT_FALSE(parsed.ok()) << path << " was accepted";
  return parsed.ok() ? std::string() : parsed.status().message();
}

std::string GrantKey(const std::string& text) {
  auto parsed = ParseGrantLocation(text);
  EXPECT_TRUE(parsed.ok()) << text << ": " << parsed.status().ToString();
  return parsed.ok() ? parsed->second : std::string();
}

bool Covers(const std::string& location, const std::string& path) {
  auto key = ParseGrantLocation(location);
  if (!key.ok()) return false;
  return LocationCovers(key->first, key->second, Parse(path), {});
}

TEST(StorageLocations, KindsAndPrivileges) {
  EXPECT_EQ(Parse("/data/a.csv").kind, StorageKind::kFilesystem);
  EXPECT_EQ(Parse("file:///data/a.csv").kind, StorageKind::kFilesystem);
  for (const char* path : {"s3://b/k", "s3a://b/k", "s3n://b/k", "gs://b/k", "gcs://b/k", "r2://b/k", "az://c/k",
                           "azure://c/k", "abfss://c@acct.dfs.core.windows.net/k"}) {
    EXPECT_EQ(Parse(path).kind, StorageKind::kObjectStorage) << path;
  }
  for (const char* path : {"http://h/x", "https://h/x", "hf://datasets/u/r/x"}) {
    EXPECT_EQ(Parse(path).kind, StorageKind::kHttp) << path;
  }
  EXPECT_EQ(StoragePrivilege(StorageKind::kObjectStorage, StorageMode::kWrite), SystemPrivilege::kWriteObjectStorage);
  EXPECT_EQ(StorageOf(SystemPrivilege::kReadHttp)->first, StorageKind::kHttp);
  EXPECT_FALSE(StorageOf(SystemPrivilege::kManageConfig));
  // The original table keeps what 2.1 knows; the new four go to the new one.
  EXPECT_TRUE(IsOriginalSystemPrivilege(SystemPrivilege::kReadFilesystem));
  EXPECT_TRUE(IsOriginalSystemPrivilege(SystemPrivilege::kCheckpoint));
  EXPECT_FALSE(IsOriginalSystemPrivilege(SystemPrivilege::kReadObjectStorage));
  EXPECT_FALSE(IsOriginalSystemPrivilege(SystemPrivilege::kWriteHttp));
}

TEST(StorageLocations, RemoteKeysFoldAliasesAndCase) {
  EXPECT_EQ(Parse("s3a://Bucket/Key/x").keys.front(), "s3://bucket/Key/x");
  EXPECT_EQ(Parse("gcs://b/k/").keys.front(), "gs://b/k");
  EXPECT_EQ(Parse("azure://c/k").keys.front(), "az://c/k");
  EXPECT_EQ(Parse("https://Host:443/p").keys.front(), "https://host/p");
  EXPECT_EQ(Parse("http://host:80/p").keys.front(), "http://host/p");
  EXPECT_EQ(Parse("https://host:8443/p").keys.front(), "https://host:8443/p");
  // A web query or fragment is the server's: not part of what is compared.
  EXPECT_EQ(Parse("https://host/p/x.csv?page=2#top").keys.front(), "https://host/p/x.csv");
  // Azure's spellings of one container meet in the protection keys.
  EXPECT_EQ(Parse("az://acct.blob.core.windows.net/c/k").other_keys.front(), "az://c/k");
  EXPECT_EQ(Parse("abfss://c@acct.dfs.core.windows.net/k").other_keys.front(), "az://c/k");
}

TEST(StorageLocations, AmbiguousRemotePathsAreRefused) {
  EXPECT_NE(Refusal("s3://b/in/../out/x").find("'..'"), std::string::npos);
  EXPECT_NE(Refusal("s3://b/in/./x").find("'.'"), std::string::npos);
  EXPECT_NE(Refusal("s3://b/in//x").find("empty path segment"), std::string::npos);
  EXPECT_NE(Refusal("s3://b/in/x?s3_endpoint=evil").find("'?' or '#'"), std::string::npos);
  EXPECT_NE(Refusal("gs://b/in/x#frag").find("'?' or '#'"), std::string::npos);
  EXPECT_NE(Refusal("s3://b/in/%2e%2e/x").find("percent-escape"), std::string::npos);
  EXPECT_NE(Refusal("s3://b/in\\x").find("backslash"), std::string::npos);
  EXPECT_NE(Refusal("https://h/p/%2E%2E/x").find("escaped"), std::string::npos);
  EXPECT_NE(Refusal("https://h/p/%2fx").find("escaped"), std::string::npos);
  EXPECT_NE(Refusal("https://user@h/p").find("user information"), std::string::npos);
  EXPECT_NE(Refusal("https://h.example@evil.example/p").find("user information"), std::string::npos);
  EXPECT_NE(Refusal("s3:///k").find("no bucket"), std::string::npos);
  EXPECT_NE(Refusal("ftp://h/x").find("the scheme ftp://"), std::string::npos);
  EXPECT_NE(Refusal("quack://h/x").find("the scheme quack://"), std::string::npos);
  // An ordinary escape on the web is the server's business.
  EXPECT_TRUE(ParseStoragePath("https://h/p/a%20b.csv", {}, true).ok());
}

TEST(StorageLocations, GlobsNameTheirFixedDirectory) {
  auto glob = Parse("s3://b/in/2026-*/x.csv");
  EXPECT_TRUE(glob.pattern);
  EXPECT_EQ(glob.keys.front(), "s3://b/in");
  EXPECT_EQ(Parse("s3://b/*").keys.front(), "s3://b");
  // Without patterns, '*' is part of a name; on the web it always is.
  EXPECT_FALSE(Parse("s3://b/in/*.csv", false).pattern);
  EXPECT_FALSE(Parse("https://h/p/*.csv").pattern);
}

TEST(StorageLocations, ALocationCoversItselfAndWhatIsBelowIt) {
  EXPECT_TRUE(Covers("s3://b/in/", "s3://b/in/a.csv"));
  EXPECT_TRUE(Covers("s3://b/in", "s3://b/in"));
  EXPECT_TRUE(Covers("s3://b/in", "s3a://B/in/deep/a.csv"));
  EXPECT_TRUE(Covers("s3://b/in", "s3://b/in/*.csv"));
  EXPECT_FALSE(Covers("s3://b/in", "s3://b/in2/a.csv"));  // a prefix of a name is no directory
  EXPECT_FALSE(Covers("s3://b/in", "s3://b/*/a.csv"));    // a glob from above
  EXPECT_FALSE(Covers("s3://b/in", "gs://b/in/a.csv"));
  EXPECT_FALSE(Covers("s3://b/in", "https://b/in/a.csv"));
  EXPECT_TRUE(Covers("https://h/api/", "https://h/api/v1?x=1"));
  EXPECT_FALSE(Covers("https://h/api/", "http://h/api/v1"));
  EXPECT_FALSE(Covers("https://h/api/", "https://h.evil/api/v1"));
  EXPECT_FALSE(Covers("az://acct.blob.core.windows.net/c", "az://other.blob.core.windows.net/c/k"));
}

TEST(StorageLocations, GrantLocations) {
  EXPECT_EQ(GrantKey("s3://Bucket/in/"), "s3://bucket/in");
  EXPECT_EQ(GrantKey("https://h/api"), "https://h/api");
  EXPECT_EQ(ParseGrantLocation("/data/in/")->first, StorageKind::kFilesystem);
  for (const char* bad : {"", "relative/dir", "~/data", "/data/*", "s3://b/in/*", "https://h/p?x=1", "s3://b/../x",
                          "ftp://h/x"}) {
    EXPECT_FALSE(ParseGrantLocation(bad).ok()) << bad;
  }
}

TEST(StorageLocations, LocationWithin) {
  EXPECT_TRUE(LocationWithin("", ""));
  EXPECT_TRUE(LocationWithin("", "s3://b/in"));
  EXPECT_FALSE(LocationWithin("s3://b/in", ""));
  EXPECT_TRUE(LocationWithin("s3://b/in", "s3://b/in/sub"));
  EXPECT_TRUE(LocationWithin("s3://b/in", "s3://b/in"));
  EXPECT_FALSE(LocationWithin("s3://b/in", "s3://b/in2"));
  EXPECT_FALSE(LocationWithin("s3://b/in/sub", "s3://b/in"));
}

TEST(StorageLocations, OverlapWithAStorageArea) {
  const auto lake = Parse("s3://b/lakes/a/", false);
  EXPECT_TRUE(StorageOverlaps(Parse("s3://b/lakes/a/main/t/f.parquet"), false, lake));
  EXPECT_TRUE(StorageOverlaps(Parse("s3a://B/lakes/a/main/t/f.parquet"), false, lake));
  EXPECT_TRUE(StorageOverlaps(Parse("s3://b/lakes/*/main/*.parquet"), false, lake));  // a glob from above
  EXPECT_TRUE(StorageOverlaps(Parse("s3://b/lakes", false), true, lake));            // a directory written above
  EXPECT_FALSE(StorageOverlaps(Parse("s3://b/lakes", false), false, lake));          // one file, beside it
  EXPECT_FALSE(StorageOverlaps(Parse("s3://b/lakes/ab/x"), false, lake));
  EXPECT_FALSE(StorageOverlaps(Parse("s3://b/other/x.csv"), true, lake));
  // An Azure container under another spelling.
  const auto azure = Parse("az://acct.blob.core.windows.net/c/lake/", false);
  EXPECT_TRUE(StorageOverlaps(Parse("abfss://c@acct.dfs.core.windows.net/lake/main/f.parquet"), false, azure));
  EXPECT_TRUE(StorageOverlaps(Parse("az://c/lake/main/f.parquet"), false, azure));
}

class LocalLocationsTest : public ::testing::Test {
 protected:
  void SetUp() override {
    root_ = fs::temp_directory_path() / ("hardcharger_locations_" + std::to_string(std::random_device{}()));
    fs::create_directories(root_ / "shared");
    fs::create_directories(root_ / "secret");
    std::ofstream(root_ / "shared" / "a.csv") << "x\n1\n";
    std::ofstream(root_ / "secret" / "s.csv") << "s\n";
    fs::create_directory_symlink(root_ / "secret", root_ / "shared" / "link");
    root_ = fs::canonical(root_);
  }
  void TearDown() override {
    std::error_code ec;
    fs::remove_all(root_, ec);
  }
  std::string At(const std::string& relative) const { return (root_ / relative).string(); }
  fs::path root_;
};

TEST_F(LocalLocationsTest, ASymlinkDoesNotLeadOutOfALocation) {
  EXPECT_TRUE(Covers(At("shared"), At("shared/a.csv")));
  EXPECT_TRUE(Covers(At("shared"), "file://" + At("shared/a.csv")));
  EXPECT_TRUE(Covers(At("shared"), At("shared/*.csv")));
  EXPECT_FALSE(Covers(At("shared"), At("shared/link/s.csv")));
  EXPECT_FALSE(Covers(At("shared"), At("shared/../secret/s.csv")));
  EXPECT_FALSE(Covers(At("shared"), At("secret/s.csv")));
  // A location reached through a symlink covers what the symlink leads to.
  EXPECT_TRUE(Covers(At("shared/link"), At("secret/s.csv")));
}

TEST_F(LocalLocationsTest, OverlapIsBySpellingAndBySymlink) {
  const auto area = Parse(At("secret"), false);
  EXPECT_TRUE(StorageOverlaps(Parse(At("shared/link/s.csv")), false, area));
  EXPECT_TRUE(StorageOverlaps(Parse(At("shared/../secret/s.csv")), false, area));
  EXPECT_TRUE(StorageOverlaps(Parse(At("*")), false, area));
  EXPECT_FALSE(StorageOverlaps(Parse(At("shared/a.csv")), false, area));
}

}  // namespace
