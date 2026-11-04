#include "geometry.hpp"

#include <bit>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <regex>
#include <stdexcept>

#include <boost/json.hpp>

namespace hardcharger::client {

namespace {

constexpr int kMaxDepth = 64;

class WkbReader {
 public:
  explicit WkbReader(std::string_view bytes) : bytes_(bytes) {}
  bool done() const { return at_ == bytes_.size(); }

  uint8_t Byte() {
    Need(1);
    return static_cast<uint8_t>(bytes_[at_++]);
  }
  uint32_t Uint(bool little) {
    Need(4);
    uint32_t value = 0;
    std::memcpy(&value, bytes_.data() + at_, 4);
    at_ += 4;
    return Swap(value, little);
  }
  double Double(bool little) {
    Need(8);
    uint64_t bits = 0;
    std::memcpy(&bits, bytes_.data() + at_, 8);
    at_ += 8;
    return std::bit_cast<double>(Swap(bits, little));
  }
  // A count, checked against the bytes left (each item takes at least `min_bytes`).
  uint32_t Count(bool little, std::size_t min_bytes) {
    const uint32_t n = Uint(little);
    if (static_cast<uint64_t>(n) * min_bytes > bytes_.size() - at_) throw std::runtime_error("count past the end");
    return n;
  }

 private:
  void Need(std::size_t n) const {
    if (at_ + n > bytes_.size()) throw std::runtime_error("ends early");
  }
  template <typename T>
  static T Swap(T value, bool little) {
    if ((std::endian::native == std::endian::little) == little) return value;
    T swapped = 0;
    for (std::size_t i = 0; i < sizeof(T); i++) {
      swapped = static_cast<T>((swapped << 8) | (value & 0xFF));
      value >>= 8;
    }
    return swapped;
  }

  std::string_view bytes_;
  std::size_t at_ = 0;
};

const char* KindName(GeometryKind kind) {
  switch (kind) {
    case GeometryKind::kPoint: return "POINT";
    case GeometryKind::kLineString: return "LINESTRING";
    case GeometryKind::kPolygon: return "POLYGON";
    case GeometryKind::kMultiPoint: return "MULTIPOINT";
    case GeometryKind::kMultiLineString: return "MULTILINESTRING";
    case GeometryKind::kMultiPolygon: return "MULTIPOLYGON";
    case GeometryKind::kCollection: return "GEOMETRYCOLLECTION";
  }
  return "";
}

Geometry ReadGeometry(WkbReader& r, int depth) {
  if (depth > kMaxDepth) throw std::runtime_error("nested too deep");
  const uint8_t order = r.Byte();
  if (order > 1) throw std::runtime_error("not WKB (byte order)");
  const bool little = order == 1;
  uint32_t code = r.Uint(little);
  // EWKB (PostGIS): flags in the high bits, and an SRID after the type when 0x20000000 is set.
  bool z = (code & 0x80000000u) != 0;
  bool m = (code & 0x40000000u) != 0;
  if (code & 0x20000000u) r.Uint(little);
  code &= 0x0FFFFFFFu;
  // ISO: 1000s for Z, 2000s for M, 3000s for ZM.
  const uint32_t iso = code / 1000;
  if (iso > 3) throw std::runtime_error("not WKB (type " + std::to_string(code) + ")");
  if (iso == 1 || iso == 3) z = true;
  if (iso == 2 || iso == 3) m = true;
  const uint32_t kind = code % 1000;
  Geometry g;
  g.has_z = z;
  g.has_m = m;
  const int width = g.Width();
  auto point = [&](std::vector<double>& into) {
    for (int i = 0; i < width; i++) into.push_back(r.Double(little));
  };
  auto points = [&](std::vector<double>& into) {
    const uint32_t n = r.Count(little, static_cast<std::size_t>(width) * 8);
    into.reserve(static_cast<std::size_t>(n) * width);
    for (uint32_t i = 0; i < n; i++) point(into);
  };
  switch (kind) {
    case 1: {
      g.kind = GeometryKind::kPoint;
      point(g.points);
      // An empty point is written with NaN coordinates.
      bool all_nan = true;
      for (double v : g.points) all_nan = all_nan && std::isnan(v);
      if (all_nan) {
        g.empty_point = true;
        g.points.clear();
      }
      return g;
    }
    case 2:
      g.kind = GeometryKind::kLineString;
      points(g.points);
      return g;
    case 3: {
      g.kind = GeometryKind::kPolygon;
      const uint32_t n = r.Count(little, 4);
      g.rings.resize(n);
      for (uint32_t i = 0; i < n; i++) points(g.rings[i]);
      return g;
    }
    case 4:
    case 5:
    case 6:
    case 7: {
      g.kind = kind == 4   ? GeometryKind::kMultiPoint
               : kind == 5 ? GeometryKind::kMultiLineString
               : kind == 6 ? GeometryKind::kMultiPolygon
                           : GeometryKind::kCollection;
      const uint32_t n = r.Count(little, 5);
      g.parts.reserve(n);
      for (uint32_t i = 0; i < n; i++) g.parts.push_back(ReadGeometry(r, depth + 1));
      if (kind != 7) {
        const GeometryKind part_kind = kind == 4   ? GeometryKind::kPoint
                                       : kind == 5 ? GeometryKind::kLineString
                                                   : GeometryKind::kPolygon;
        for (const auto& part : g.parts) {
          if (part.kind != part_kind) throw std::runtime_error(std::string("a ") + KindName(g.kind) + " of other types");
        }
      }
      return g;
    }
    default:
      throw std::runtime_error("unknown WKB geometry type " + std::to_string(code));
  }
}

std::string CoordsText(const double* coords, int width) {
  std::string out;
  for (int i = 0; i < width; i++) {
    if (i) out += ' ';
    out += CoordText(coords[i]);
  }
  return out;
}

// "(x y, x y)" for a run of points, or EMPTY for none.
std::string ListText(const std::vector<double>& points, int width) {
  if (points.empty()) return "EMPTY";
  std::string out = "(";
  for (std::size_t i = 0; i + width <= points.size(); i += width) {
    if (i) out += ", ";
    out += CoordsText(points.data() + i, width);
  }
  return out + ")";
}

// The geometry's body: what follows its tag ("(1 2)", "EMPTY", ...).
std::string Body(const Geometry& g) {
  const int width = g.Width();
  switch (g.kind) {
    case GeometryKind::kPoint:
      return g.empty_point || g.points.empty() ? "EMPTY" : "(" + CoordsText(g.points.data(), width) + ")";
    case GeometryKind::kLineString:
      return ListText(g.points, width);
    case GeometryKind::kPolygon: {
      if (g.rings.empty()) return "EMPTY";
      std::string out = "(";
      for (std::size_t i = 0; i < g.rings.size(); i++) {
        if (i) out += ", ";
        out += ListText(g.rings[i], width);
      }
      return out + ")";
    }
    case GeometryKind::kMultiPoint: {
      // DuckDB writes MULTIPOINT (1 2, 3 4), without parentheses around each point.
      if (g.parts.empty()) return "EMPTY";
      std::string out = "(";
      for (std::size_t i = 0; i < g.parts.size(); i++) {
        if (i) out += ", ";
        const Geometry& p = g.parts[i];
        out += p.kind == GeometryKind::kPoint && !p.empty_point && !p.points.empty()
                   ? CoordsText(p.points.data(), p.Width())
                   : "EMPTY";
      }
      return out + ")";
    }
    case GeometryKind::kMultiLineString:
    case GeometryKind::kMultiPolygon: {
      if (g.parts.empty()) return "EMPTY";
      std::string out = "(";
      for (std::size_t i = 0; i < g.parts.size(); i++) {
        if (i) out += ", ";
        out += Body(g.parts[i]);
      }
      return out + ")";
    }
    case GeometryKind::kCollection: {
      if (g.parts.empty()) return "EMPTY";
      std::string out = "(";
      for (std::size_t i = 0; i < g.parts.size(); i++) {
        if (i) out += ", ";
        out += ToWkt(g.parts[i]);
      }
      return out + ")";
    }
  }
  return "";
}

}  // namespace

Geometry ParseWkb(std::string_view bytes) {
  WkbReader r(bytes);
  Geometry g = ReadGeometry(r, 0);
  if (!r.done()) throw std::runtime_error("bytes after the geometry");
  return g;
}

std::string CoordText(double value) {
  if (std::isnan(value)) return "nan";
  if (std::isinf(value)) return value > 0 ? "inf" : "-inf";
  if (value == 0) return std::signbit(value) ? "-0" : "0";
  // The shortest digits that read back as the same double, in scientific form
  // ("1.2345e+17", "5e-324"), then laid out as DuckDB does.
  char buf[64];
  const auto result = std::to_chars(buf, buf + sizeof(buf), value, std::chars_format::scientific);
  std::string_view text(buf, static_cast<std::size_t>(result.ptr - buf));
  const bool negative = text.front() == '-';
  if (negative) text.remove_prefix(1);
  const std::size_t e_at = text.find('e');
  const std::string_view mantissa = text.substr(0, e_at);
  int exponent = 0;
  const char* exponent_text = text.data() + e_at + 1;
  if (*exponent_text == '+') exponent_text++;  // from_chars takes no leading plus
  std::from_chars(exponent_text, text.data() + text.size(), exponent);
  std::string out;
  if (exponent < -4 || exponent >= 16) {
    out = std::string(mantissa) + "e" + (exponent < 0 ? "-" : "+");
    const std::string digits = std::to_string(std::abs(exponent));
    if (digits.size() < 2) out += '0';
    out += digits;
  } else {
    std::string digits;
    for (char c : mantissa) {
      if (c != '.') digits += c;
    }
    const int n = static_cast<int>(digits.size());
    if (exponent < 0) {
      out = "0." + std::string(static_cast<std::size_t>(-exponent - 1), '0') + digits;
    } else if (n <= exponent + 1) {
      out = digits + std::string(static_cast<std::size_t>(exponent + 1 - n), '0');
    } else {
      out = digits.substr(0, static_cast<std::size_t>(exponent + 1)) + "." + digits.substr(static_cast<std::size_t>(exponent + 1));
    }
  }
  return negative ? "-" + out : out;
}

std::string ToWkt(const Geometry& g) {
  std::string out = KindName(g.kind);
  if (g.has_z && g.has_m) out += " ZM";
  else if (g.has_z) out += " Z";
  else if (g.has_m) out += " M";
  return out + " " + Body(g);
}

std::string WkbText(std::string_view wkb) {
  try {
    return ToWkt(ParseWkb(wkb));
  } catch (const std::exception& error) {
    return std::string("[invalid WKB: ") + error.what() + "]";
  }
}

std::string BlobText(std::string_view bytes) {
  static constexpr char kHex[] = "0123456789ABCDEF";
  std::string out;
  out.reserve(bytes.size());
  for (unsigned char c : bytes) {
    if (c >= 32 && c <= 126 && c != '\\' && c != '\'' && c != '"') {
      out += static_cast<char>(c);
    } else {
      out += "\\x";
      out += kHex[c >> 4];
      out += kHex[c & 0x0F];
    }
  }
  return out;
}

std::string GeometryTypeName(std::string_view extension_metadata) {
  static const std::regex kAuthorityCode("^[A-Za-z]+:[A-Za-z0-9_.-]+$");
  std::string label;
  boost::system::error_code ec;
  const boost::json::value parsed = boost::json::parse(extension_metadata.empty() ? "{}" : extension_metadata, ec);
  if (!ec && parsed.is_object()) {
    if (const auto* crs = parsed.as_object().if_contains("crs")) {
      if (crs->is_string()) {
        label = std::string(crs->as_string());
      } else if (crs->is_object()) {
        // PROJJSON: its id's authority and code ("EPSG", 4326).
        if (const auto* id = crs->as_object().if_contains("id"); id && id->is_object()) {
          const auto* authority = id->as_object().if_contains("authority");
          const auto* code = id->as_object().if_contains("code");
          if (authority && authority->is_string() && code) {
            if (code->is_string()) label = std::string(authority->as_string()) + ":" + std::string(code->as_string());
            else if (code->is_int64()) label = std::string(authority->as_string()) + ":" + std::to_string(code->as_int64());
            else if (code->is_uint64()) label = std::string(authority->as_string()) + ":" + std::to_string(code->as_uint64());
          }
        }
      }
    }
  }
  if (!label.empty() && std::regex_match(label, kAuthorityCode)) return "GEOMETRY('" + label + "')";
  return "GEOMETRY";
}

}  // namespace hardcharger::client
