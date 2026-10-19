// The CPU model hardcharger_server_info() and the instances row report on Linux, from
// /proc/cpuinfo's text: x86's "model name", a board's "Model", and ARM's implementer and
// part ids, which is all most ARM kernels give (AWS Graviton, Azure Cobalt).
#include <gtest/gtest.h>

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
