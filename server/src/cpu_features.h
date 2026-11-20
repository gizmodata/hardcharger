/*
 * The x86-64 microarchitecture levels a CPU supports, from CPUID (issue #62). Header-only
 * (static inline), for the check before main() (cpu_check.c, C) and the baseline build's
 * guard on downloaded DuckDB extensions (authz/extension_cpu_guard.cc, C++). Must be compiled
 * without -march flags; Hardcharger's own code is.
 *
 * hardcharger_cpu_missing(level, buf, cap) writes the features of x86-64-v2 (level 2) or
 * x86-64-v2 and -v3 (level 3) this CPU lacks into buf, space-separated with a leading space,
 * and returns how many. Defined on x86_64 Linux only (HARDCHARGER_HAVE_CPU_FEATURES).
 */
#ifndef HARDCHARGER_CPU_FEATURES_H
#define HARDCHARGER_CPU_FEATURES_H

#if defined(__x86_64__) && defined(__linux__)
#define HARDCHARGER_HAVE_CPU_FEATURES 1

#include <cpuid.h>
#include <stddef.h>
#include <string.h>

struct hardcharger_cpu_feature {
  const char *name;
  int level;
  unsigned leaf;
  int reg; /* 1 = ebx, 2 = ecx, 3 = edx */
  unsigned bit;
};

/* x86-64-v2 and -v3, as the x86-64 psABI defines them, named as Linux's /proc/cpuinfo does
 * (abm = LZCNT, pni = SSE3). AVX also needs the OS to save the AVX registers (OSXSAVE and
 * XCR0), checked separately: "os-avx-state". */
static const struct hardcharger_cpu_feature hardcharger_cpu_features[] = {
    {"cx16", 2, 1, 2, 13},  {"lahf_lm", 2, 0x80000001u, 2, 0}, {"popcnt", 2, 1, 2, 23}, {"pni", 2, 1, 2, 0},
    {"sse4_1", 2, 1, 2, 19}, {"sse4_2", 2, 1, 2, 20},           {"ssse3", 2, 1, 2, 9},   {"avx", 3, 1, 2, 28},
    {"avx2", 3, 7, 1, 5},    {"bmi1", 3, 7, 1, 3},              {"bmi2", 3, 7, 1, 8},    {"f16c", 3, 1, 2, 29},
    {"fma", 3, 1, 2, 12},    {"abm", 3, 0x80000001u, 2, 5},     {"movbe", 3, 1, 2, 22},  {"xsave", 3, 1, 2, 26},
};

static inline int hardcharger_cpuid(unsigned leaf, unsigned regs[4]) {
  unsigned top = __get_cpuid_max(leaf & 0x80000000u, 0);
  if (top < leaf) return 0;
  __cpuid_count(leaf, 0, regs[0], regs[1], regs[2], regs[3]);
  return 1;
}

static inline void hardcharger_cpu_append(char *buf, size_t *len, size_t cap, const char *text) {
  size_t n = strlen(text);
  if (*len + n >= cap) n = cap - 1 - *len;
  memcpy(buf + *len, text, n);
  *len += n;
  buf[*len] = '\0';
}

static inline int hardcharger_cpu_missing(int level, char *buf, size_t cap) {
  size_t len = 0;
  int missing = 0;
  buf[0] = '\0';
  for (size_t i = 0; i < sizeof hardcharger_cpu_features / sizeof hardcharger_cpu_features[0]; i++) {
    const struct hardcharger_cpu_feature *f = &hardcharger_cpu_features[i];
    if (f->level > level) continue;
    unsigned regs[4] = {0, 0, 0, 0};
    if (!hardcharger_cpuid(f->leaf, regs) || !(regs[f->reg] & (1u << f->bit))) {
      hardcharger_cpu_append(buf, &len, cap, " ");
      hardcharger_cpu_append(buf, &len, cap, f->name);
      missing++;
    }
  }
  if (level >= 3) {
    unsigned regs[4] = {0, 0, 0, 0};
    int os_avx = 0;
    if (hardcharger_cpuid(1, regs) && (regs[2] & (1u << 27))) {
      unsigned lo, hi;
      __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
      (void)hi;
      os_avx = (lo & 6u) == 6u;
    }
    if (!os_avx) {
      hardcharger_cpu_append(buf, &len, cap, " os-avx-state");
      missing++;
    }
  }
  return missing;
}

#endif
#endif
