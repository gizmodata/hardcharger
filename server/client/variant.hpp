#pragma once

#include <string>
#include <string_view>

namespace hardcharger::client {

struct VariantValue {
  std::string text;
  std::string json;
};

// Parquet VARIANT v1. Returns an explicit diagnostic for invalid/newer encodings;
// it never mistakes an ordinary Arrow struct for VARIANT.
VariantValue DecodeVariant(std::string_view metadata, std::string_view value);

}  // namespace hardcharger::client
