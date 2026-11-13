// Play distribution: register only the extensions linked into the signed application.
// DuckDB's loadable_extensions and httplib capabilities are deliberately absent.
#include "duckdb_static_extension.h"

#define BUNDLED_EXTENSIONS(X) \
  X(autocomplete) X(tpch) X(httpfs) X(ducklake) X(postgres_scanner) \
  X(core_functions) X(parquet) X(json) X(icu)

#define DECLARE_EXTENSION(name) \
  int32_t duckdb_extension_##name##_describe(duckdb_extension_descriptor *descriptor);
BUNDLED_EXTENSIONS(DECLARE_EXTENSION)

__attribute__((visibility("hidden"))) int32_t duckdb_register_static_extensions(void) {
  int32_t result = 0;
#define REGISTER_EXTENSION(name) \
  if (duckdb_register_static_extension(duckdb_extension_##name##_describe) != 0) result = 1;
  BUNDLED_EXTENSIONS(REGISTER_EXTENSION)
  return result;
}
