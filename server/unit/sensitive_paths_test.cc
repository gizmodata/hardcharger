// The sensitive path policy: ports the 5 unit cases of GizmoSQL 1.x's
// tests/integration/test_sensitive_path_guard.cpp against Hardcharger's SensitivePaths.
#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <optional>
#include <random>
#include <string>

#include "guards.hpp"
#include "platform.hpp"

namespace fs = std::filesystem;
using hardcharger::SensitivePaths;

namespace {

class SensitivePathPolicyTest : public ::testing::Test {
 protected:
  void SetUp() override {
    root_ = fs::temp_directory_path() / ("hardcharger_spg_" + std::to_string(std::random_device{}()));
    fs::create_directories(home() / ".ssh");
    fs::create_directories(root_ / "data");
    std::ofstream(root_ / "data" / "ok.csv") << "a\n1\n";
    std::ofstream(home() / ".ssh" / "key") << "x";
    root_ = fs::canonical(root_);
    policy_ = std::make_unique<SensitivePaths>(true, std::vector<std::string>{(root_ / "extra").string()},
                                               std::vector<std::string>{(root_ / "server.key").string()},
                                               home().string());
    cwd_ = fs::current_path();
  }
  void TearDown() override {
    fs::current_path(cwd_);
    std::error_code ec;
    fs::remove_all(root_, ec);
  }
  fs::path home() const { return root_ / "home"; }
  std::optional<std::string> Check(const fs::path& p) const { return policy_->Check(p.string()); }

  fs::path root_;
  fs::path cwd_;
  std::unique_ptr<SensitivePaths> policy_;
};

}  // namespace

TEST_F(SensitivePathPolicyTest, DefaultCategories) {
  EXPECT_EQ(Check(home() / ".ssh" / "key"), "user credential store");
  EXPECT_EQ(Check(home() / ".aws" / "credentials"), "user credential store");
  EXPECT_EQ(Check(home() / ".duckdb" / "stored_secrets" / "s.duckdb_secret"), "DuckDB persistent secrets");
  EXPECT_EQ(Check(root_ / "server.key"), "Hardcharger server credentials");
  EXPECT_EQ(Check(root_ / "extra" / "any" / "file"), "operator-protected path");
#ifdef _WIN32
  const std::string windows = hardcharger::EnvUtf8("SystemRoot").value_or("C:\\Windows");
  EXPECT_EQ(Check(windows + "\\System32\\config\\SAM"), "system account files");
  EXPECT_EQ(Check(windows + "\\Panther\\unattend.xml"), "system account files");
  EXPECT_EQ(Check("\\\\.\\pipe\\some-pipe"), "Windows device namespace");
  EXPECT_EQ(Check("C:\\var\\run\\secrets\\kubernetes.io\\token"), "container and Kubernetes secrets");
#else
  EXPECT_EQ(Check("/etc/passwd"), "system account files");
  EXPECT_EQ(Check("/proc/self/environ"), "process information (/proc)");
  EXPECT_EQ(Check("/run/secrets/token"), "container and Kubernetes secrets");
#endif
}

#ifdef _WIN32
// Windows' credential stores, its spellings of one path, and the shares that reach a
// drive over SMB.
TEST_F(SensitivePathPolicyTest, WindowsStoresAndSpellings) {
  const fs::path roaming = home() / "AppData" / "Roaming";
  EXPECT_EQ(Check(roaming / "Microsoft" / "Credentials" / "ABCDEF"), "Windows credential and key store");
  EXPECT_EQ(Check(roaming / "Microsoft" / "Protect" / "S-1-5-21" / "key"), "Windows credential and key store");
  EXPECT_EQ(Check(home() / "AppData" / "Local" / "Microsoft" / "Vault" / "x"), "Windows credential and key store");
  EXPECT_EQ(Check(roaming / "postgresql" / "pgpass.conf"), "user credential store");
  EXPECT_EQ(Check(home() / "_netrc"), "user credential store");

  const std::string key = (home() / ".ssh" / "key").string();  // C:\...\.ssh\key
  auto respelled = [&](const std::string& from, const std::string& to) {
    std::string out = key;
    out.replace(out.find(from), from.size(), to);
    return out;
  };
  EXPECT_TRUE(policy_->Check(respelled(".ssh", ".SSH")).has_value());          // case
  EXPECT_TRUE(policy_->Check(fs::path(key).generic_string()).has_value());     // '/'
  EXPECT_TRUE(policy_->Check("\\\\?\\" + key).has_value());                  // \\?\C:\...
  EXPECT_TRUE(policy_->Check("\\\\.\\" + key).has_value());                  // \\.\C:\...
  EXPECT_TRUE(policy_->Check(respelled(".ssh", ".ssh.")).has_value());         // trailing dot
  EXPECT_TRUE(policy_->Check(respelled(".ssh", ".ssh  ")).has_value());        // trailing spaces
  EXPECT_TRUE(policy_->Check(key + "::$DATA").has_value());                    // the default stream
  EXPECT_TRUE(policy_->Check(key + ":other").has_value());                     // another stream
  EXPECT_TRUE(policy_->Check("file:///" + fs::path(key).generic_string()).has_value());
  EXPECT_TRUE(policy_->Check("file:/" + fs::path(key).generic_string()).has_value());
  EXPECT_EQ(Check("\\\\localhost\\C$\\Users\\someone\\.ssh\\id_rsa"), "Windows administrative share");
  EXPECT_EQ(Check("//127.0.0.1/admin$/System32/config/SAM"), "Windows administrative share");
  EXPECT_EQ(Check("\\\\?\\UNC\\localhost\\c$\\x"), "Windows administrative share");
  EXPECT_EQ(Check("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x"), "Windows device namespace");
  EXPECT_FALSE(Check("\\\\fileserver.invalid\\data\\sales.csv").has_value());
}
#else
TEST_F(SensitivePathPolicyTest, OnlyOwnCgroupFileIsReadableInProc) {
  EXPECT_FALSE(Check("/proc/self/cgroup").has_value());
  EXPECT_EQ(Check("/proc/self/environ"), "process information (/proc)");
  EXPECT_EQ(Check("/proc/1/cgroup"), "process information (/proc)");
}

// DuckDB opens file:/x (one slash) as /x too.
TEST_F(SensitivePathPolicyTest, EveryFileUrlForm) {
  const auto key = (home() / ".ssh" / "key").generic_string();
  EXPECT_TRUE(policy_->Check("file:" + key).has_value());
  EXPECT_TRUE(policy_->Check("file://" + key).has_value());
  EXPECT_TRUE(policy_->Check("file://localhost" + key).has_value());
  EXPECT_EQ(policy_->Check("file:/etc/passwd"), "system account files");
}
#endif

TEST_F(SensitivePathPolicyTest, OrdinaryPathsAreAllowed) {
  EXPECT_FALSE(Check(root_ / "data" / "ok.csv").has_value());
  EXPECT_FALSE(Check(home() / "notes.txt").has_value());
  EXPECT_FALSE(Check(home() / ".sshfoo").has_value());  // a sibling, not inside .ssh
  EXPECT_FALSE(Check("/etc/passwd2").has_value());
  EXPECT_FALSE(Check("/procfs/x").has_value());
  EXPECT_FALSE(policy_->Check("s3://bucket/etc/passwd").has_value());
  EXPECT_FALSE(policy_->Check("https://example.com/.ssh/key").has_value());
}

TEST_F(SensitivePathPolicyTest, OtherSpellingsResolveToTheSamePath) {
  const auto key = home() / ".ssh" / "key";
  EXPECT_TRUE(Check(root_ / "data" / ".." / "home" / ".ssh" / "key").has_value());
  EXPECT_TRUE(policy_->Check("file://" + key.generic_string()).has_value());
  std::error_code ec;
  fs::create_symlink(key, root_ / "data" / "innocent.csv", ec);
  ASSERT_FALSE(ec) << ec.message();
  EXPECT_TRUE(Check(root_ / "data" / "innocent.csv").has_value());
  fs::current_path(root_ / "data");
  EXPECT_TRUE(policy_->Check("../home/.ssh/key").has_value());
#if defined(__APPLE__) || defined(_WIN32)
  std::string upper = key.string();
  upper.replace(upper.find(".ssh"), 4, ".SSH");
  EXPECT_TRUE(policy_->Check(upper).has_value());
#endif
  // Hardcharger: ~ is the server user's home.
  EXPECT_EQ(policy_->Check("~/.ssh/key"), "user credential store");
}

// The same file under a name no spelling shows: a second hard link of a protected file.
TEST_F(SensitivePathPolicyTest, HardLinksToProtectedFilesAreRefused) {
  std::ofstream(home() / ".netrc") << "machine example.invalid password dummy";
  std::error_code ec;
  fs::create_hard_link(home() / ".netrc", root_ / "data" / "notes.txt", ec);
  ASSERT_FALSE(ec) << ec.message();
  EXPECT_EQ(Check(root_ / "data" / "notes.txt"), "user credential store");
  // The positive twin: a file with one link, and a second link of an ordinary file.
  EXPECT_FALSE(Check(root_ / "data" / "ok.csv").has_value());
  fs::create_hard_link(root_ / "data" / "ok.csv", root_ / "data" / "ok-again.csv", ec);
  ASSERT_FALSE(ec) << ec.message();
  EXPECT_FALSE(Check(root_ / "data" / "ok-again.csv").has_value());
}

TEST_F(SensitivePathPolicyTest, SecretDirectoryAddedAfterStartup) {
  const auto dir = root_ / "custom_secrets";
  EXPECT_FALSE(Check(dir / "s.duckdb_secret").has_value());
  policy_->AddSecretDirectory(dir.string());
  EXPECT_EQ(Check(dir / "s.duckdb_secret"), "DuckDB persistent secrets");
}

// The file system checks the paths DuckDB opens, never a statement's literals; still, a
// string that only looks like a pattern, opened as a file of that name in the server's
// home (the image's working directory, with ~/.ssh in it), is no protected path. DuckDB's
// glob has no brace expansion (FileSystem::HasGlob: * ? [ only), so '{' is an ordinary
// character too.
TEST_F(SensitivePathPolicyTest, BracesAreOrdinaryCharacters) {
  fs::current_path(home());
  for (const char* literal : {"{\"a\": 1}", "{\"a\": {\"b\": [1, 2]}}", "{}", "{x}", "^[0-9]{3}$", "a{2,3}",
                              "[0-9]{2,4}-[a-z]{1}", "\\d{3}-\\d{4}", "{}-{}", "{.ssh,.aws}", "{*}", "*", ".*", "{:,}",
                              "(\\d{2})", "[0-9a-f]{64}", "**", "?", "[.]ssh"}) {
    EXPECT_FALSE(policy_->Check(literal).has_value()) << literal;
  }
#ifndef _WIN32
  // '/etc/{passwd,hosts}' is one file of that name for DuckDB, not /etc/passwd.
  EXPECT_FALSE(Check("/etc/{passwd,hosts}").has_value());
#endif
  EXPECT_FALSE(Check(home() / "{.ssh,.aws}" / "key").has_value());
}

// A glob pattern is checked as the path it spells: refused when its fixed part is
// protected. One reaching into a protected path from above is not (the file system drops
// those matches instead, file_guard.cc).
TEST_F(SensitivePathPolicyTest, PatternsAreCheckedByTheirFixedPart) {
  fs::current_path(home());
  EXPECT_EQ(Check(home() / ".ssh" / "{key,other}"), "user credential store");  // inside .ssh
  EXPECT_EQ(policy_->Check(".ssh/{key}"), "user credential store");           // relative to the working directory
  EXPECT_EQ(Check(home() / ".ssh" / "*"), "user credential store");
  EXPECT_EQ(policy_->Check("~/.ssh/**/*"), "user credential store");
  EXPECT_FALSE(Check(home() / "*" / "key").has_value());
  EXPECT_FALSE(Check(home() / ".ss?" / "key").has_value());
  EXPECT_FALSE(policy_->Check("**/key").has_value());
  EXPECT_FALSE(Check(root_ / "data" / "*.csv").has_value());
#ifndef _WIN32
  EXPECT_EQ(Check("/etc/{passwd,hosts}/../passwd"), "system account files");  // .. past a braced name
#endif
}
