// The CPU model hardcharger_server_info() and the instances row report on Linux, from
// /proc/cpuinfo's text: x86's "model name", a board's "Model", and ARM's implementer and
// part ids, which is all most ARM kernels give (AWS Graviton, Azure Cobalt).
#include <gtest/gtest.h>

#include <cmath>
#include <filesystem>
#include <fstream>

#include "platform.hpp"

using hardcharger::CpuModelFromCpuinfo;

TEST(CpuModelFromCpuinfo, X86ModelName) {
  EXPECT_EQ(CpuModelFromCpuinfo("processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel\t\t: 17\n"
                                "model name\t: AMD EPYC 9R14 96-Core Processor\nflags\t\t: fpu\n"),
            "AMD EPYC 9R14 96-Core Processor");
}

TEST(CpuModelFromCpuinfo, ModelLineOfABoard) {
  EXPECT_EQ(CpuModelFromCpuinfo("processor\t: 0\nCPU implementer\t: 0x41\nCPU part\t: 0xd08\n\n"
                                "Hardware\t: BCM2835\nModel\t\t: Raspberry Pi 4 Model B Rev 1.4\n"),
            "Raspberry Pi 4 Model B Rev 1.4");
}

TEST(CpuModelFromCpuinfo, ArmImplementerAndPart) {
  const std::string graviton3 =
      "processor\t: 0\nBogoMIPS\t: 2100.00\nFeatures\t: fp asimd evtstrm\nCPU implementer\t: 0x41\n"
      "CPU architecture: 8\nCPU variant\t: 0x1\nCPU part\t: 0xd40\nCPU revision\t: 1\n\n"
      "processor\t: 1\nCPU implementer\t: 0x41\nCPU part\t: 0xd40\n";
  EXPECT_EQ(CpuModelFromCpuinfo(graviton3), "ARM Neoverse-V1");
  EXPECT_EQ(CpuModelFromCpuinfo("CPU implementer\t: 0x41\r\nCPU part\t: 0xd49\r\n"), "ARM Neoverse-N2");
  EXPECT_EQ(CpuModelFromCpuinfo("CPU implementer\t: 0xc0\nCPU part\t: 0xac3\n"), "Ampere AmpereOne");
}

TEST(CpuModelFromCpuinfo, UnknownArmPartKeepsTheIds) {
  EXPECT_EQ(CpuModelFromCpuinfo("CPU implementer\t: 0x41\nCPU part\t: 0xd99\n"), "ARM implementer 0x41 part 0xd99");
  EXPECT_EQ(CpuModelFromCpuinfo("CPU implementer\t: 0x51\nCPU part\t: 0x001\n"),
            "Qualcomm implementer 0x51 part 0x001");
}

TEST(CpuModelFromCpuinfo, NothingToGo) {
  EXPECT_EQ(CpuModelFromCpuinfo(""), "");
  EXPECT_EQ(CpuModelFromCpuinfo("processor\t: 0\nBogoMIPS\t: 50.00\n"), "");
  // A key that only starts like one ("Model name"-like prefixes) isn't taken.
  EXPECT_EQ(CpuModelFromCpuinfo("Modelx\t: no\nmodel\t\t: 17\n"), "");
  EXPECT_EQ(CpuModelFromCpuinfo("CPU implementer\t: 0x41\n"), "");
}

// The container limits behind hardcharger_cgroup_memory_limit_bytes and
// hardcharger_cgroup_cpu_quota_cores, read from a scratch cgroup file system.
namespace {

class CGroupDirectory {
 public:
  CGroupDirectory()
      : root_(std::filesystem::temp_directory_path() /
              ("hardcharger-cgroup-test-" + std::to_string(hardcharger::ProcessId()) + "-" +
               ::testing::UnitTest::GetInstance()->current_test_info()->name())) {
    std::filesystem::remove_all(root_);
    std::filesystem::create_directories(root_);
  }
  ~CGroupDirectory() {
    std::error_code ec;
    std::filesystem::remove_all(root_, ec);
  }
  void Write(const std::string& relative, const std::string& text) const {
    const auto path = root_ / relative;
    std::filesystem::create_directories(path.parent_path());
    std::ofstream(path) << text;
  }
  std::string root() const { return hardcharger::Utf8Path(root_); }

 private:
  std::filesystem::path root_;
};

}  // namespace

using hardcharger::ReadCGroupLimits;

TEST(CGroupLimits, Version2Limits) {
  CGroupDirectory dir;
  dir.Write("memory.max", "2147483648\n");
  dir.Write("cpu.max", "150000 100000\n");
  const auto limits = ReadCGroupLimits(dir.root());
  EXPECT_EQ(limits.memory_limit_bytes, 2147483648.0);
  EXPECT_DOUBLE_EQ(limits.cpu_quota_cores, 1.5);
}

TEST(CGroupLimits, Version2WithoutLimits) {
  CGroupDirectory dir;
  dir.Write("memory.max", "max\n");
  dir.Write("cpu.max", "max 100000\n");
  // v1 files beside them are not read: memory.max exists, so this is cgroup v2.
  dir.Write("memory/memory.limit_in_bytes", "1073741824\n");
  const auto limits = ReadCGroupLimits(dir.root());
  EXPECT_TRUE(std::isnan(limits.memory_limit_bytes));
  EXPECT_TRUE(std::isnan(limits.cpu_quota_cores));
}

TEST(CGroupLimits, Version1Limits) {
  CGroupDirectory dir;
  dir.Write("memory/memory.limit_in_bytes", "536870912\n");
  dir.Write("cpu/cpu.cfs_quota_us", "50000\n");
  dir.Write("cpu/cpu.cfs_period_us", "100000\n");
  const auto limits = ReadCGroupLimits(dir.root());
  EXPECT_EQ(limits.memory_limit_bytes, 536870912.0);
  EXPECT_DOUBLE_EQ(limits.cpu_quota_cores, 0.5);
}

TEST(CGroupLimits, Version1WithoutLimits) {
  CGroupDirectory dir;
  // "No limit" in v1 is PAGE_COUNTER_MAX rounded to the page size, and a quota of -1.
  dir.Write("memory/memory.limit_in_bytes", "9223372036854771712\n");
  dir.Write("cpu/cpu.cfs_quota_us", "-1\n");
  dir.Write("cpu/cpu.cfs_period_us", "100000\n");
  const auto limits = ReadCGroupLimits(dir.root());
  EXPECT_TRUE(std::isnan(limits.memory_limit_bytes));
  EXPECT_TRUE(std::isnan(limits.cpu_quota_cores));
}

TEST(CGroupLimits, NoCGroupFileSystemOrGarbage) {
  CGroupDirectory dir;
  auto limits = ReadCGroupLimits(dir.root() + "/missing");
  EXPECT_TRUE(std::isnan(limits.memory_limit_bytes));
  EXPECT_TRUE(std::isnan(limits.cpu_quota_cores));
  dir.Write("memory.max", "lots\n");
  dir.Write("cpu.max", "100000\n");  // no period
  limits = ReadCGroupLimits(dir.root());
  EXPECT_TRUE(std::isnan(limits.memory_limit_bytes));
  EXPECT_TRUE(std::isnan(limits.cpu_quota_cores));
}

// The process_* readings are known on Linux, macOS and Windows (open descriptors aside
// on Windows, which has handles instead).
TEST(ProcessStats, AreKnownAndSane) {
  const double now = static_cast<double>(std::time(nullptr));
  const double started = hardcharger::ProcessStartTimeSeconds();
  EXPECT_LE(started, now + 1);
  EXPECT_GT(started, now - 3600);
  EXPECT_GE(hardcharger::ProcessThreads(), 1);
#ifndef _WIN32
  EXPECT_GE(hardcharger::ProcessOpenFds(), 3);  // stdin, stdout, stderr at least
#endif
}
