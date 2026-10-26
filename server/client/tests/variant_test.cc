#include "variant.hpp"

#include <arrow/array/array_nested.h>
#include <arrow/array/builder_binary.h>
#include <arrow/buffer.h>
#include <arrow/extension/parquet_variant.h>
#include <arrow/extension_type.h>
#include <gtest/gtest.h>

#include <boost/json.hpp>
#include <fstream>
#include <sstream>

#include "output_renderer.hpp"

namespace hardcharger::client {
namespace {
std::string Bytes(std::string_view hex) {
  std::string result;
  for (size_t i = 0; i < hex.size(); i += 2)
    result += static_cast<char>(std::stoi(std::string(hex.substr(i, 2)), nullptr, 16));
  return result;
}
}  // namespace

TEST(VariantRendererTest, SharedEncodingExamples) {
  std::ifstream in(HARDCHARGER_VARIANT_FIXTURES);
  ASSERT_TRUE(in.good());
  std::stringstream contents;
  contents << in.rdbuf();
  for (const auto& item : boost::json::parse(contents.str()).as_array()) {
    const auto& example = item.as_object();
    SCOPED_TRACE(std::string(example.at("name").as_string()));
    const auto decoded =
        DecodeVariant(Bytes(example.at("metadata").as_string()), Bytes(example.at("value").as_string()));
    EXPECT_EQ(decoded.text, example.at("text").as_string());
    EXPECT_EQ(decoded.json, example.at("json").as_string());
  }
}

TEST(VariantRendererTest, ExtensionAndOutputModes) {
  RegisterArrowExtensionTypes();
  arrow::BinaryBuilder metadata, value;
  ASSERT_TRUE(metadata.Append(Bytes("010000")).ok());
  ASSERT_TRUE(value.Append(Bytes("142a000000")).ok());
  ASSERT_TRUE(metadata.AppendNull().ok());
  ASSERT_TRUE(value.AppendNull().ok());
  const auto m = metadata.Finish().ValueOrDie(), v = value.Finish().ValueOrDie();
  const auto storage = arrow::StructArray::Make({m, v},
                                                std::vector<std::shared_ptr<arrow::Field>>{
                                                    arrow::field("metadata", arrow::binary(), false),
                                                    arrow::field("value", arrow::binary(), false)},
                                                arrow::Buffer::FromString(std::string(1, '\1')), 1)
                           .ValueOrDie();
  const auto type = arrow::extension::variant(storage->type());
  const auto array = arrow::ExtensionType::WrapArray(type, storage);
  const auto column = std::make_shared<arrow::ChunkedArray>(array);
  EXPECT_EQ(GetCellValue(column, 0, "NULL"), "42");
  EXPECT_EQ(GetCellValue(column, 1, "NULL"), "NULL");
  EXPECT_EQ(FriendlyTypeName(type), "variant");
  EXPECT_NE(GetCellValue(std::make_shared<arrow::ChunkedArray>(storage), 0, "NULL"), "42");
  const auto table = arrow::Table::Make(arrow::schema({arrow::field("v", type)}), {column});
  for (const auto mode : {OutputMode::JSON, OutputMode::JSONLINES}) {
    auto renderer = CreateRenderer(mode, ClientConfig{});
    std::ostringstream out;
    renderer->Render(*table, out);
    EXPECT_NE(out.str().find("\"v\": 42"), std::string::npos);
    EXPECT_EQ(out.str().find("metadata"), std::string::npos);
  }
}
}  // namespace hardcharger::client
