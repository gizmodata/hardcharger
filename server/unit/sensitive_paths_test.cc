// The sensitive path policy: ports the 5 unit cases of GizmoSQL 1.x's
// tests/integration/test_sensitive_path_guard.cpp against Hardcharger's SensitivePaths.
#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <optional>
#include <random>
#include <string>

#include "guards.hpp"

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
  EXPECT_EQ(Check("/etc/passwd"), "system account files");
  EXPECT_EQ(Check("/proc/self/environ"), "process information (/proc)");
  EXPECT_EQ(Check("/run/secrets/token"), "container and Kubernetes secrets");
}

TEST_F(SensitivePathPolicyTest, OnlyOwnCgroupFileIsReadableInProc) {
  EXPECT_FALSE(Check("/proc/self/cgroup").has_value());
  EXPECT_EQ(Check("/proc/self/environ"), "process information (/proc)");
  EXPECT_EQ(Check("/proc/1/cgroup"), "process information (/proc)");
}

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
#if defined(__APPLE__)
  std::string upper = key.string();
  upper.replace(upper.find(".ssh"), 4, ".SSH");
  EXPECT_TRUE(policy_->Check(upper).has_value());
#endif
  // Hardcharger: ~ is the server user's home, and patterns that reach a protected path.
  EXPECT_TRUE(policy_->Check((home() / "*" / "key").string()).has_value());
  EXPECT_FALSE(policy_->Check((root_ / "data" / "*.csv").string()).has_value());
}

TEST_F(SensitivePathPolicyTest, SecretDirectoryAddedAfterStartup) {
  const auto dir = root_ / "custom_secrets";
  EXPECT_FALSE(Check(dir / "s.duckdb_secret").has_value());
  policy_->AddSecretDirectory(dir.string());
  EXPECT_EQ(Check(dir / "s.duckdb_secret"), "DuckDB persistent secrets");
}
