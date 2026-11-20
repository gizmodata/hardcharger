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
#include "cpu_features.h"

#if defined(HARDCHARGER_HAVE_CPU_FEATURES) && defined(HARDCHARGER_CPU_REQUIRE)

#include <unistd.h>

#ifndef HARDCHARGER_CPU_BASELINE_NAME
#error "HARDCHARGER_CPU_BASELINE_NAME names the baseline download"
#endif

static void hardcharger_cpu_check(int argc, char **argv, char **envp) {
  (void)argc;
  (void)envp;
  char missing[256];
  if (hardcharger_cpu_missing(HARDCHARGER_CPU_REQUIRE, missing, sizeof missing) == 0) return;

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
