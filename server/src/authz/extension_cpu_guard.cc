// The baseline Linux x86_64 build (issue #62) runs on any x86-64 CPU, but the extensions
// DuckDB publishes for download (extensions.duckdb.org) are compiled for newer ones: on a
// CPU without x86-64-v3 (AVX2), loading one dies with "Illegal instruction", and since it
// runs inside the server, that ends every session (httpfs, autoloaded by any https:// or
// s3:// path, crashed the server on QEMU's kvm64 CPU model). So on such a CPU this build
// installs and loads no extension at all from outside the binary: DuckDB's extension
// provider is replaced by one that refuses both, with a message that says why and names
// the extensions built into this build. Those load as they always do, and INSTALL of one
// (DuckDB's usual recipe, `INSTALL httpfs; LOAD httpfs`) succeeds without downloading
// anything, as DuckDB calls the provider for it too. On a CPU that has x86-64-v3, nothing
// changes.
#include <duckdb.hpp>
#include <duckdb/common/exception.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/database.hpp>
#include <duckdb/main/extension/external_extension_provider.hpp>
#include <duckdb/main/extension_helper.hpp>
#include <duckdb/main/extension_install_info.hpp>

#include <string>

#include "authz/authz.hpp"
#include "cpu_features.h"

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;

class RefusingExtensionProvider : public dd::ExternalExtensionProvider {
 public:
  RefusingExtensionProvider(std::string missing, std::string built_in)
      : missing_(std::move(missing)), built_in_(std::move(built_in)) {}

  std::string GetName() const override { return "hardcharger-baseline"; }
  bool SupportsExternalExtensions() const override { return true; }

  dd::unique_ptr<dd::ExtensionInstallInfo> Install(dd::DatabaseInstance& db, dd::FileSystem& fs, const std::string&,
                                                   const std::string& extension, dd::ExtensionInstallOptions&,
                                                   dd::optional_ptr<dd::ClientContext>) override {
    // The name DuckDB's own install would use (an alias such as postgres, or a path's base name).
    const auto name = dd::ExtensionHelper::ApplyExtensionAlias(fs.ExtractBaseName(extension));
    if (db.ExtensionIsLoaded(name)) {
      auto info = dd::make_uniq<dd::ExtensionInstallInfo>();
      info->mode = dd::ExtensionInstallMode::STATICALLY_LINKED;
      return info;
    }
    throw dd::IOException(Message(extension));
  }
  void* OpenLibrary(const std::string& filename, const std::string&) override {
    throw dd::IOException(Message(filename));
  }
  void* TryLoadFunction(void*, const std::string&) override { return nullptr; }
  std::string GetLibraryError() override { return "external extensions are not loaded on this CPU"; }

 private:
  std::string Message(const std::string& extension) const {
    return "Extension \"" + extension +
           "\" can't be installed or loaded: this is Hardcharger's baseline build, which runs on any "
           "x86-64 CPU, but DuckDB's downloadable extensions need an x86-64-v3 CPU (AVX2), and this one "
           "lacks:" +
           missing_ + ". The extensions built into this build work as usual: " + built_in_ +
           ". On a CPU (or a virtual machine CPU model) with AVX2, use the optimized build, which "
           "loads every extension.";
  }

  std::string missing_;
  std::string built_in_;
};

}  // namespace

bool BaselineCpuLacksV3() {
#if defined(HARDCHARGER_BASELINE_BUILD) && defined(HARDCHARGER_HAVE_CPU_FEATURES)
  char missing[256];
  return hardcharger_cpu_missing(3, missing, sizeof missing) > 0;
#else
  return false;
#endif
}

bool InstallExtensionCpuGuard(duckdb_v2_instance_handle instance, const std::string& built_in) {
#if defined(HARDCHARGER_BASELINE_BUILD) && defined(HARDCHARGER_HAVE_CPU_FEATURES)
  char missing[256];
  if (hardcharger_cpu_missing(3, missing, sizeof missing) == 0) return false;
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  auto& config = dd::DBConfig::GetConfig(database);
  config.SetExternalExtensionProvider(dd::make_shared_ptr<RefusingExtensionProvider>(missing, built_in));
  return true;
#else
  (void)instance;
  (void)built_in;
  return false;
#endif
}

}  // namespace hardcharger::authz
