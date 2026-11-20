/*
 * Refuses to start, with a message, on an x86_64 CPU without the instructions this build was
 * compiled for, instead of dying with "Illegal instruction" in the first compiled code that
 * uses one (issue #62).
 *
 * The optimized Linux x86_64 build compiles DuckDB for x86-64-v3 (AVX2: DuckDB's default
 * optimization profile) and Arrow for x86-64-v2 (SSE4.2: Arrow's default SIMD level), and
 * DuckDB's code runs before main(): its built-in extensions register from static
 * initializers. So the check runs from the program's .preinit_array, which the C library
 * calls before any other initializer, of the program or of a shared library it loads. This
 * file itself must be compiled without those instructions; Hardcharger's own code is built
 * without -march flags.
 *
 * HARDCHARGER_CPU_REQUIRE is the x86-64 microarchitecture level the program needs (2 or 3),
 * and HARDCHARGER_CPU_BASELINE_NAME the release download that runs on any x86-64 CPU. Neither
 * set (the baseline build, other architectures, macOS, Windows): this file compiles to nothing.
 */
#if defined(__x86_64__) && defined(__linux__) && defined(HARDCHARGER_CPU_REQUIRE)

#include <cpuid.h>
#include <string.h>
#include <unistd.h>

#ifndef HARDCHARGER_CPU_BASELINE_NAME
#error "HARDCHARGER_CPU_BASELINE_NAME names the baseline download"
#endif

struct hardcharger_cpu_feature {
  const char *name;
  int level;
  unsigned leaf;
  int reg; /* 1 = ebx, 2 = ecx, 3 = edx */
  unsigned bit;
};

/* x86-64-v2 and -v3, as the x86-64 psABI defines them. AVX also needs the OS to save the
 * AVX registers (OSXSAVE and XCR0), checked separately below. */
static const struct hardcharger_cpu_feature hardcharger_cpu_features[] = {
    {"cx16", 2, 1, 2, 13},       {"lahf_lm", 2, 0x80000001u, 2, 0}, {"popcnt", 2, 1, 2, 23},
    {"sse3", 2, 1, 2, 0},        {"sse4_1", 2, 1, 2, 19},           {"sse4_2", 2, 1, 2, 20},
    {"ssse3", 2, 1, 2, 9},       {"avx", 3, 1, 2, 28},              {"avx2", 3, 7, 1, 5},
    {"bmi1", 3, 7, 1, 3},        {"bmi2", 3, 7, 1, 8},              {"f16c", 3, 1, 2, 29},
    {"fma", 3, 1, 2, 12},        {"abm", 3, 0x80000001u, 2, 5},     {"movbe", 3, 1, 2, 22},
    {"xsave", 3, 1, 2, 26},
};

static int hardcharger_cpuid(unsigned leaf, unsigned regs[4]) {
  unsigned top = __get_cpuid_max(leaf & 0x80000000u, 0);
  if (top < leaf) return 0;
  __cpuid_count(leaf, 0, regs[0], regs[1], regs[2], regs[3]);
  return 1;
}

static void hardcharger_cpu_append(char *buf, size_t *len, size_t cap, const char *text) {
  size_t n = strlen(text);
  if (*len + n >= cap) n = cap - 1 - *len;
  memcpy(buf + *len, text, n);
  *len += n;
  buf[*len] = '\0';
}

static void hardcharger_cpu_check(int argc, char **argv, char **envp) {
  (void)argc;
  (void)envp;
  char missing[256];
  size_t missing_len = 0;
  missing[0] = '\0';
  for (size_t i = 0; i < sizeof hardcharger_cpu_features / sizeof hardcharger_cpu_features[0]; i++) {
    const struct hardcharger_cpu_feature *f = &hardcharger_cpu_features[i];
    if (f->level > HARDCHARGER_CPU_REQUIRE) continue;
    unsigned regs[4] = {0, 0, 0, 0};
    if (!hardcharger_cpuid(f->leaf, regs) || !(regs[f->reg] & (1u << f->bit))) {
      hardcharger_cpu_append(missing, &missing_len, sizeof missing, " ");
      hardcharger_cpu_append(missing, &missing_len, sizeof missing, f->name);
    }
  }
#if HARDCHARGER_CPU_REQUIRE >= 3
  {
    /* The OS must have enabled saving the SSE and AVX registers (XCR0 bits 1 and 2). */
    unsigned regs[4] = {0, 0, 0, 0};
    int os_avx = 0;
    if (hardcharger_cpuid(1, regs) && (regs[2] & (1u << 27))) {
      unsigned lo, hi;
      __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
      os_avx = (lo & 6u) == 6u;
    }
    if (!os_avx) hardcharger_cpu_append(missing, &missing_len, sizeof missing, " os-avx-state");
  }
#endif
  if (missing_len == 0) return;

  const char *program = (argv && argv[0]) ? argv[0] : "hardcharger";
  const char *slash = strrchr(program, '/');
  if (slash) program = slash + 1;
  char message[1024];
  size_t len = 0;
  message[0] = '\0';
  hardcharger_cpu_append(message, &len, sizeof message, program);
#if HARDCHARGER_CPU_REQUIRE >= 3
  hardcharger_cpu_append(message, &len, sizeof message,
                         ": this build needs an x86-64-v3 CPU (AVX2: Intel Haswell, AMD Excavator or newer),");
#else
  hardcharger_cpu_append(message, &len, sizeof message,
                         ": this build needs an x86-64-v2 CPU (SSE4.2: Intel Nehalem, AMD Bulldozer or newer),");
#endif
  hardcharger_cpu_append(message, &len, sizeof message, " and this one lacks:");
  hardcharger_cpu_append(message, &len, sizeof message, missing);
  hardcharger_cpu_append(message, &len, sizeof message,
                         ".\nDownload the baseline build, which runs on any x86-64 CPU: " HARDCHARGER_CPU_BASELINE_NAME
                         "\n(a virtual machine may hide its host CPU's features: see its CPU model setting).\n");
  ssize_t ignored = write(STDERR_FILENO, message, len);
  (void)ignored;
  _exit(1);
}

__attribute__((section(".preinit_array"), used)) static void (*const hardcharger_cpu_check_entry)(int, char **, char **) =
    hardcharger_cpu_check;

#else
/* Nothing to check here; keep the translation unit non-empty (ISO C). */
typedef int hardcharger_cpu_check_unused;
#endif
