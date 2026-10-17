// Bulk ingest of the Arrow types pyarrow can't build (day_time intervals), through the
// SDK's DuckDB ingest: part of GizmoSQL 1.x's test_bulk_ingest.cpp
// (ArrowTypesIngestAsTheirDuckDBTypes' day_time column, and the nested refusal of
// UnsupportedArrowTypeIsRefusedNotStoredAsVarchar), run from server/tests/test_bulk_ingest.py.
#include <gtest/gtest.h>

#include <arrow/api.h>

#include "duckdb_sql.hpp"
#include "duckdb_upload.hpp"
#include "duckdb_v2.hpp"
#include "warpsql/errors.hpp"

namespace {

struct Db {
  warpduckdb::Environment environment;
  warpduckdb::Instance instance;
  warpduckdb::Connection connection;
  Db() {
    warpduckdb::Call([&](auto* e) { return duckdb_v2_environment_create(environment.out(), e); });
    warpduckdb::Call([&](auto* e) {
      return duckdb_v2_instance_create_with_options(environment.get(), nullptr, nullptr, 0, instance.out(), e);
    });
    warpduckdb::Call([&](auto* e) { return duckdb_v2_connection_create(instance.get(), connection.out(), e); });
    warpduckdb::RegisterUploadFunction(connection.get());
    warpduckdb::RunSql(connection.get(), "ATTACH ':memory:' AS ingest_db; USE ingest_db");
  }
};

std::shared_ptr<arrow::RecordBatchReader> OneColumn(const std::string& name, std::shared_ptr<arrow::Array> values) {
  auto schema = arrow::schema({arrow::field(name, values->type())});
  return *arrow::RecordBatchReader::Make({arrow::RecordBatch::Make(schema, values->length(), {values})}, schema);
}

std::shared_ptr<arrow::Array> DayTime(int32_t days, int32_t ms) {
  arrow::DayTimeIntervalBuilder builder;
  EXPECT_TRUE(builder.Append({days, ms}).ok());
  return *builder.Finish();
}

}  // namespace

TEST(IngestTypes, TopLevelDayTimeIntervalIngestsAsInterval) {
  Db db;
  warpsql::IngestTarget target{.table = "day_time_ingest", .mode = warpsql::IngestMode::kReplace};
  EXPECT_EQ(warpduckdb::Ingest(db.connection.get(), target, OneColumn("day_time", DayTime(1, 3600000))), 1);
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(),
                                  "SELECT data_type FROM information_schema.columns WHERE table_name = 'day_time_ingest'"),
            "INTERVAL");
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(), "SELECT day_time::VARCHAR FROM day_time_ingest"),
            "1 day 01:00:00");
}

TEST(IngestTypes, NestedDayTimeIntervalIsRefused) {
  Db db;
  auto values = DayTime(1, 3600000);
  arrow::Int32Builder offsets_builder;
  EXPECT_TRUE(offsets_builder.AppendValues({0, 1}).ok());
  auto offsets = *offsets_builder.Finish();
  auto list = *arrow::ListArray::FromArrays(*offsets, *values);
  warpsql::IngestTarget target{.table = "nested_day_time_ingest", .mode = warpsql::IngestMode::kReplace};
  try {
    warpduckdb::Ingest(db.connection.get(), target, OneColumn("l", list));
    FAIL() << "a nested day_time interval was ingested";
  } catch (const warpsql::Error& error) {
    EXPECT_EQ(error.code(), warpsql::ErrorCode::kUnsupportedArrowType);
    EXPECT_NE(std::string(error.what()).find("a day_time interval nested in another type cannot be ingested"),
              std::string::npos)
        << error.what();
  }
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(),
                                  "SELECT count(*) FROM information_schema.tables WHERE table_name = 'nested_day_time_ingest'"),
            "0");
}

TEST(IngestTypes, MonthIntervalIngestsAsInterval) {
  Db db;
  arrow::MonthIntervalBuilder builder;
  ASSERT_TRUE(builder.Append(14).ok());
  warpsql::IngestTarget target{.table = "month_ingest", .mode = warpsql::IngestMode::kReplace};
  EXPECT_EQ(warpduckdb::Ingest(db.connection.get(), target, OneColumn("months", *builder.Finish())), 1);
  EXPECT_EQ(warpduckdb::QueryText(db.connection.get(), "SELECT months::VARCHAR FROM month_ingest"), "1 year 2 months");
}
