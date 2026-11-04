// GEOMETRY and BLOB as text (issue #22): the WKB reader on well-formed and malformed input,
// DuckDB's number layout, and DuckDB's BLOB escaping. The server-side twin,
// tests/test_client_cli_geometry.py, compares the CLI's output with ST_AsText on a real
// server for every geometry type and output format.
#include "geometry.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

namespace hardcharger::client {
namespace {

// Little-endian (or big-endian) WKB: a byte order, a type code, counts, then doubles.
std::string Wkb(uint32_t code, const std::vector<double>& doubles, bool little = true,
                const std::vector<uint32_t>& counts = {}) {
  std::string out;
  out += static_cast<char>(little ? 1 : 0);
  auto put = [&](auto value) {
    unsigned char bytes[sizeof(value)];
    std::memcpy(bytes, &value, sizeof(value));
    if ((std::endian::native == std::endian::little) != little) std::reverse(bytes, bytes + sizeof(value));
    out.append(reinterpret_cast<const char*>(bytes), sizeof(value));
  };
  put(code);
  for (uint32_t count : counts) put(count);
  for (double d : doubles) put(d);
  return out;
}

TEST(Geometry, PointsInEveryDimensionAndByteOrder) {
  EXPECT_EQ(WkbText(Wkb(1, {1, 2})), "POINT (1 2)");
  EXPECT_EQ(WkbText(Wkb(1, {1, 2}, /*little=*/false)), "POINT (1 2)");
  EXPECT_EQ(WkbText(Wkb(1001, {1, 2, 3})), "POINT Z (1 2 3)");
  EXPECT_EQ(WkbText(Wkb(2001, {1, 2, 3})), "POINT M (1 2 3)");
  EXPECT_EQ(WkbText(Wkb(3001, {1, 2, 3, 4})), "POINT ZM (1 2 3 4)");
  // EWKB: the Z and M flags in the high bits, and an SRID after the type.
  EXPECT_EQ(WkbText(Wkb(0x80000001u, {1, 2, 3})), "POINT Z (1 2 3)");
  EXPECT_EQ(WkbText(Wkb(0x40000001u, {1, 2, 3})), "POINT M (1 2 3)");
  EXPECT_EQ(WkbText(Wkb(0x20000001u, {1, 2}, true, {4326})), "POINT (1 2)");
  const double nan = std::numeric_limits<double>::quiet_NaN();
  EXPECT_EQ(WkbText(Wkb(1, {nan, nan})), "POINT EMPTY");
  EXPECT_EQ(WkbText(Wkb(1001, {nan, nan, nan})), "POINT Z EMPTY");
}

TEST(Geometry, LinesPolygonsMultisAndCollections) {
  EXPECT_EQ(WkbText(Wkb(2, {0, 0, 1, 1, 2, 0.5}, true, {3})), "LINESTRING (0 0, 1 1, 2 0.5)");
  EXPECT_EQ(WkbText(Wkb(2, {}, true, {0})), "LINESTRING EMPTY");
  // A polygon: a ring count, then each ring's point count and points.
  std::string polygon = Wkb(3, {}, true, {2});
  polygon += Wkb(0, {0, 0, 10, 0, 10, 10, 0, 0}, true, {4}).substr(5);  // the ring: count + points
  polygon += Wkb(0, {2, 2, 2, 4, 4, 4, 2, 2}, true, {4}).substr(5);
  EXPECT_EQ(WkbText(polygon), "POLYGON ((0 0, 10 0, 10 10, 0 0), (2 2, 2 4, 4 4, 2 2))");
  EXPECT_EQ(WkbText(Wkb(3, {}, true, {0})), "POLYGON EMPTY");
  // Multis hold whole geometries, each with its own byte order and type.
  EXPECT_EQ(WkbText(Wkb(4, {}, true, {2}) + Wkb(1, {1, 2}) + Wkb(1, {3, 4}, false)), "MULTIPOINT (1 2, 3 4)");
  EXPECT_EQ(WkbText(Wkb(4, {}, true, {0})), "MULTIPOINT EMPTY");
  EXPECT_EQ(WkbText(Wkb(5, {}, true, {2}) + Wkb(2, {}, true, {0}) + Wkb(2, {0, 0, 1, 1}, true, {2})),
            "MULTILINESTRING (EMPTY, (0 0, 1 1))");
  EXPECT_EQ(WkbText(Wkb(6, {}, true, {1}) + Wkb(3, {}, true, {0})), "MULTIPOLYGON (EMPTY)");
  EXPECT_EQ(WkbText(Wkb(7, {}, true, {2}) + Wkb(1, {1, 2}) + Wkb(7, {}, true, {1}) + Wkb(4, {}, true, {1}) + Wkb(1, {1, 1})),
            "GEOMETRYCOLLECTION (POINT (1 2), GEOMETRYCOLLECTION (MULTIPOINT (1 1)))");
  EXPECT_EQ(WkbText(Wkb(2007, {}, true, {1}) + Wkb(2001, {1, 2, 3})), "GEOMETRYCOLLECTION M (POINT M (1 2 3))");
  EXPECT_EQ(WkbText(Wkb(7, {}, true, {0})), "GEOMETRYCOLLECTION EMPTY");
}

TEST(Geometry, NumbersPrintAsDuckDBDoes) {
  EXPECT_EQ(CoordText(1), "1");
  EXPECT_EQ(CoordText(-80.8431), "-80.8431");
  EXPECT_EQ(CoordText(1e20), "1e+20");
  EXPECT_EQ(CoordText(-1e-7), "-1e-07");
  EXPECT_EQ(CoordText(1e15), "1000000000000000");
  EXPECT_EQ(CoordText(1e16), "1e+16");
  EXPECT_EQ(CoordText(123456789012345678.0), "1.2345678901234568e+17");
  EXPECT_EQ(CoordText(0.0001), "0.0001");
  EXPECT_EQ(CoordText(0.00001), "1e-05");
  EXPECT_EQ(CoordText(-0.5), "-0.5");
  EXPECT_EQ(CoordText(-0.0), "-0");
  EXPECT_EQ(CoordText(0.0), "0");
  EXPECT_EQ(CoordText(123456789.123456789), "123456789.12345679");
  EXPECT_EQ(CoordText(0.1), "0.1");
  EXPECT_EQ(CoordText(0.30000000000000004), "0.30000000000000004");
  EXPECT_EQ(CoordText(1.5e300), "1.5e+300");
  EXPECT_EQ(CoordText(-2.5e-300), "-2.5e-300");
  EXPECT_EQ(CoordText(179.99999999999997), "179.99999999999997");
  EXPECT_EQ(CoordText(-89.123456789012345), "-89.12345678901235");  // the nearest double, shortest
  EXPECT_EQ(CoordText(std::numeric_limits<double>::infinity()), "inf");
  EXPECT_EQ(CoordText(-std::numeric_limits<double>::infinity()), "-inf");
  EXPECT_EQ(CoordText(std::numeric_limits<double>::quiet_NaN()), "nan");
}

TEST(Geometry, MalformedWkbPrintsAMarkerInsteadOfCrashing) {
  EXPECT_EQ(WkbText(""), "[invalid WKB: ends early]");
  EXPECT_EQ(WkbText(Wkb(1, {1, 2}).substr(0, 12)), "[invalid WKB: ends early]");
  EXPECT_EQ(WkbText(std::string("\x02") + Wkb(1, {1, 2}).substr(1)), "[invalid WKB: not WKB (byte order)]");
  EXPECT_EQ(WkbText(Wkb(9, {1, 2})), "[invalid WKB: unknown WKB geometry type 9]");
  EXPECT_EQ(WkbText(Wkb(5001, {1, 2})), "[invalid WKB: not WKB (type 5001)]") << "an ISO prefix past ZM";
  EXPECT_EQ(WkbText(Wkb(2, {0, 0}, true, {1000000})), "[invalid WKB: count past the end]");
  EXPECT_EQ(WkbText(Wkb(1, {1, 2}) + "x"), "[invalid WKB: bytes after the geometry]");
  EXPECT_EQ(WkbText(Wkb(4, {}, true, {1}) + Wkb(2, {}, true, {0})), "[invalid WKB: a MULTIPOINT of other types]");
  // Collections nested past the depth limit: each level is a collection of one.
  std::string deep;
  for (int i = 0; i < 70; i++) deep += Wkb(7, {}, true, {1});
  deep += Wkb(1, {1, 2});
  EXPECT_EQ(WkbText(deep), "[invalid WKB: nested too deep]");
  // Random bytes never throw out of WkbText.
  for (uint32_t seed = 1; seed < 2000; seed++) {
    std::string bytes;
    uint32_t x = seed;
    for (int i = 0; i < 40; i++) {
      x = x * 1664525u + 1013904223u;
      bytes += static_cast<char>(x >> 24);
    }
    const std::string text = WkbText(bytes);
    EXPECT_FALSE(text.empty());
  }
}

TEST(Geometry, BlobTextEscapesAsDuckDBDoes) {
  EXPECT_EQ(BlobText("abc"), "abc");
  EXPECT_EQ(BlobText(std::string("\x01\x00\xf5", 3)), "\\x01\\x00\\xF5");
  EXPECT_EQ(BlobText("a\\b'c\"d"), "a\\x5Cb\\x27c\\x22d");
  EXPECT_EQ(BlobText(std::string(" ~\x7f", 3)), " ~\\x7F");
  EXPECT_EQ(BlobText(""), "");
  for (int c = 0; c < 256; c++) {
    const std::string text = BlobText(std::string(1, static_cast<char>(c)));
    for (unsigned char byte : text) EXPECT_TRUE(byte >= 32 && byte < 127) << c;  // always printable ASCII
  }
}

TEST(Geometry, TypeNameCarriesTheCrs) {
  EXPECT_EQ(GeometryTypeName(""), "GEOMETRY");
  EXPECT_EQ(GeometryTypeName("{}"), "GEOMETRY");
  EXPECT_EQ(GeometryTypeName("not json"), "GEOMETRY");
  EXPECT_EQ(GeometryTypeName(R"({"crs": "EPSG:4326"})"), "GEOMETRY('EPSG:4326')");
  EXPECT_EQ(GeometryTypeName(R"({"crs": "OGC:CRS84"})"), "GEOMETRY('OGC:CRS84')");
  EXPECT_EQ(GeometryTypeName(R"({"crs": {"type": "GeographicCRS", "name": "WGS 84", "id": {"authority": "EPSG", "code": 4326}}})"),
            "GEOMETRY('EPSG:4326')");
  EXPECT_EQ(GeometryTypeName(R"({"crs": {"type": "GeographicCRS", "name": "WGS 84"}})"), "GEOMETRY");
  EXPECT_EQ(GeometryTypeName(R"({"crs": "a CRS with spaces"})"), "GEOMETRY");
  EXPECT_EQ(GeometryTypeName(R"({"crs": "EPSG:4326'); DROP"})"), "GEOMETRY");
}

}  // namespace
}  // namespace hardcharger::client
