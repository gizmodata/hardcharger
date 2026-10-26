// The hint a LOAD gets when the extension needs another one that isn't installed
// (iceberg needs avro; an administrator may disable automatic installation/loading).
#include <gtest/gtest.h>

#include <string>

#include "engine.hpp"

TEST(ExtensionHint, NamesWhatToInstallFirst) {
  const std::string duckdb =
      "Invalid Input Error: Initialization function \"iceberg_duckdb_cpp_init\" from file \"/x/iceberg.duckdb_extension\" "
      "threw an exception: \"The iceberg extension requires the avro extension, but it could not be loaded. Try "
      "running \"INSTALL avro; LOAD avro;\" first.";
  const auto hint = hardcharger::ExtensionDependencyHint(duckdb);
  ASSERT_TRUE(hint.has_value());
  EXPECT_EQ(hint->rfind("Hardcharger: the iceberg extension needs the avro extension installed. Run INSTALL avro; "
                        "then LOAD iceberg again",
                        0),
            0u)
      << *hint;
}

TEST(ExtensionHint, NoneForOtherErrors) {
  EXPECT_FALSE(hardcharger::ExtensionDependencyHint("Catalog Error: Table with name t does not exist!").has_value());
  EXPECT_FALSE(hardcharger::ExtensionDependencyHint("IO Error: Extension \"x\" not found.").has_value());
}
