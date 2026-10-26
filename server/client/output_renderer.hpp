// Ported from GizmoSQL 1.x src/client/output_renderer.hpp.

#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <arrow/table.h>

#include "client_config.hpp"

namespace hardcharger::client {

struct RenderResult {
  int64_t rows_rendered = 0;
  int columns_rendered = 0;
  bool footer_rendered = false;  // true if footer was rendered inside the table
};

class OutputRenderer {
 public:
  virtual ~OutputRenderer() = default;
  virtual RenderResult Render(const arrow::Table& table, std::ostream& out) = 0;

  bool show_headers = true;
  std::string null_value = "NULL";
  int max_rows = 0;      // 0 = unlimited
  int max_width = 0;     // 0 = unlimited
  int64_t total_rows = -2;  // Footer total override: -2 = not set, -1 = unknown, >= 0 = known
};

std::unique_ptr<OutputRenderer> CreateRenderer(OutputMode mode,
                                                const ClientConfig& config);

// Utility: get string value of a cell from a chunked array
std::string GetCellValue(const std::shared_ptr<arrow::ChunkedArray>& column,
                         int64_t row_index, const std::string& null_value);

// Utility: get terminal width
int GetTerminalWidth();

// Map Arrow type to DuckDB-friendly display name (e.g., "varchar", "bigint")
std::string FriendlyTypeName(const std::shared_ptr<arrow::DataType>& type);

// Registers VARIANT and arrow.opaque readers: VARIANT is decoded for display and
// JSON, and DuckDB HUGEINT/UHUGEINT retain exact integers with arrow_lossless_conversion.
// Idempotent; WarpConnection::Connect calls it.
void RegisterArrowExtensionTypes();

}  // namespace hardcharger::client
