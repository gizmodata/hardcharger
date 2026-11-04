// GEOMETRY and BLOB values as text. Hardcharger sends a GEOMETRY column as Arrow binary
// tagged geoarrow.wkb (GeoArrow), each value its Well-Known Binary, and the column's CRS
// (when it has one) as PROJJSON in the extension metadata. This decodes WKB (ISO and EWKB,
// either byte order, every dimension) into a small tree and writes it as WKT exactly as
// DuckDB's ST_AsText does; the web UI's ui/src/lib/geometry.ts is its twin, and the two
// must print the same text. BLOBs print as DuckDB's CLI prints them: printable ASCII as is,
// everything else as \xHH. https://geoarrow.org/extension-types.html
#pragma once

#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger::client {

enum class GeometryKind { kPoint, kLineString, kPolygon, kMultiPoint, kMultiLineString, kMultiPolygon, kCollection };

// A decoded geometry. Coordinates are x, y, (z), (m), `width` of them per point; an empty
// point has none. Simple kinds hold `points` (a point: one; a line: its points) or `rings`
// (a polygon); multi kinds and collections hold `parts`.
struct Geometry {
  GeometryKind kind = GeometryKind::kPoint;
  bool has_z = false;
  bool has_m = false;
  std::vector<double> points;               // point / linestring, flattened by `Width()`
  std::vector<std::vector<double>> rings;   // polygon
  std::vector<Geometry> parts;              // multi*, collection
  bool empty_point = false;                 // POINT EMPTY (written with NaN coordinates)
  int Width() const { return 2 + (has_z ? 1 : 0) + (has_m ? 1 : 0); }
};

// Decodes one WKB value; throws std::runtime_error on anything that isn't exactly one
// well-formed geometry (ends early, a count past the end, an unknown type, a part of the
// wrong kind in a multi-geometry, bytes after the geometry, nesting over 64 deep).
Geometry ParseWkb(std::string_view bytes);

// A coordinate as DuckDB prints it: the shortest text that reads back as the same double,
// with an exponent (at least two digits) below 1e-4 or from 1e16 up: 1e-05, 1e+16, -0.
std::string CoordText(double value);

// Well-Known Text, as DuckDB's ST_AsText writes it ("POINT (1 2)", "POLYGON ZM (...)",
// "MULTIPOINT EMPTY").
std::string ToWkt(const Geometry& geometry);

// WKT for `wkb`, or a marker naming the problem ("[invalid WKB: ends early]") for bytes
// that aren't one well-formed geometry: a bad value prints, it never stops the result.
std::string WkbText(std::string_view wkb);

// A BLOB as DuckDB's CLI prints it: bytes 32-126 other than \ ' " as themselves, the rest
// as \xHH (upper-case hex). Always ASCII, so it is valid UTF-8 and valid inside JSON.
std::string BlobText(std::string_view bytes);

// The type's name as DuckDB's typeof gives it: GEOMETRY, or GEOMETRY('EPSG:4326') when the
// column's GeoArrow metadata (`{"crs": ...}`, PROJJSON or an "AUTHORITY:CODE" string) names
// a CRS by an authority and code. A CRS given only as PROJJSON without an id, or that
// can't be read, is left out of the name.
std::string GeometryTypeName(std::string_view extension_metadata);

}  // namespace hardcharger::client
