#include "variant.hpp"

#include <arrow/util/decimal.h>
#include <arrow/util/utf8.h>

#include <algorithm>
#include <bit>
#include <boost/json.hpp>
#include <charconv>
#include <chrono>
#include <cmath>
#include <format>
#include <map>
#include <set>
#include <stdexcept>
#include <vector>

namespace hardcharger::client {
namespace {
// https://github.com/apache/parquet-format/blob/master/VariantEncoding.md

struct Reader {
  std::string_view bytes;
  void Check(size_t at, size_t size) const {
    if (at > bytes.size() || size > bytes.size() - at) throw std::runtime_error("truncated value");
  }
  uint64_t UInt(size_t at, size_t size) const {
    Check(at, size);
    uint64_t n = 0;
    for (size_t i = size; i > 0; --i) n = n * 256 + static_cast<uint8_t>(bytes[at + i - 1]);
    return n;
  }
  arrow::Decimal128 Int(size_t at, size_t size) const {
    Check(at, size);
    const bool negative = static_cast<uint8_t>(bytes[at + size - 1]) & 128;
    uint64_t low = UInt(at, std::min(size, size_t(8)));
    uint64_t high = size > 8 ? UInt(at + 8, size - 8) : negative ? UINT64_MAX : 0;
    if (size < 8 && negative) low |= UINT64_MAX << (size * 8);
    return arrow::Decimal128(std::bit_cast<int64_t>(high), low);
  }
  std::string Text(size_t at, size_t size) const {
    Check(at, size);
    auto text = bytes.substr(at, size);
    if (!arrow::util::ValidateUTF8(text)) throw std::runtime_error("invalid UTF-8");
    return std::string(text);
  }
};
VariantValue String(std::string text) { return {text, boost::json::serialize(boost::json::value(text))}; }
VariantValue Literal(std::string text) { return {text, text}; }

std::string Timestamp(int64_t n, bool nanos, bool utc) {
  const int64_t per_second = nanos ? 1000000000 : 1000000;
  int64_t seconds = n / per_second, rest = n % per_second;
  if (rest < 0) {
    --seconds;
    rest += per_second;
  }
  const auto point = std::chrono::sys_seconds(std::chrono::seconds(seconds));
  const auto days = std::chrono::floor<std::chrono::days>(point);
  const std::chrono::year_month_day date(days);
  if (!date.ok()) throw std::runtime_error("timestamp out of range");
  const std::chrono::hh_mm_ss clock(point - days);
  std::string fraction = std::format("{:0{}}", rest, nanos ? 9 : 6);
  while (!fraction.empty() && fraction.back() == '0') fraction.pop_back();
  return std::format("{:04}-{:02}-{:02} {:02}:{:02}:{:02}{}{}", int(date.year()), unsigned(date.month()),
                     unsigned(date.day()), clock.hours().count(), clock.minutes().count(),
                     clock.seconds().count(), fraction.empty() ? "" : "." + fraction, utc ? "+00" : "");
}

VariantValue Read(std::string_view bytes, const std::vector<std::string>& dictionary, int depth) {
  if (depth > 100) throw std::runtime_error("nesting exceeds 100 levels");
  Reader r{bytes};
  const auto tag = r.UInt(0, 1), basic = tag & 3, h = tag >> 2;
  if (basic == 1) return String(r.Text(1, h));
  if (basic == 2 || basic == 3) {
    const bool object = basic == 2;
    const size_t size_width = h & (object ? 16 : 4) ? 4 : 1;
    const size_t offset_width = (h & 3) + 1, id_width = object ? ((h >> 2) & 3) + 1 : 0;
    const size_t n = r.UInt(1, size_width), ids = 1 + size_width, offsets = ids + n * id_width;
    const size_t values = offsets + (n + 1) * offset_width;
    r.Check(0, values);
    const size_t total = r.UInt(offsets + n * offset_width, offset_width);
    r.Check(values, total);
    std::vector<size_t> starts;
    for (size_t i = 0; i < n; ++i) starts.push_back(r.UInt(offsets + i * offset_width, offset_width));
    auto physical = starts;
    physical.push_back(total);
    std::sort(physical.begin(), physical.end());
    if (physical.front() != 0 || std::adjacent_find(physical.begin(), physical.end()) != physical.end())
      throw std::runtime_error("invalid field offsets");
    std::map<size_t, size_t> ends;
    for (size_t i = 0; i < n; ++i) ends.emplace(physical[i], physical[i + 1]);
    std::set<std::string> keys;
    std::string json = object ? "{" : "[";
    for (size_t i = 0; i < n; ++i) {
      const size_t start = starts[i], end = ends.at(start);
      if (end > total || end <= start) throw std::runtime_error("invalid field offset");
      if (i) json += ',';
      if (object) {
        const auto id = r.UInt(ids + i * id_width, id_width);
        if (id >= dictionary.size() || !keys.insert(dictionary[id]).second)
          throw std::runtime_error("invalid object key");
        json += String(dictionary[id]).json + ':';
      }
      json += Read(bytes.substr(values + start, end - start), dictionary, depth + 1).json;
    }
    return Literal(json + (object ? "}" : "]"));
  }
  if (h == 0) return Literal("null");
  if (h == 1 || h == 2) return Literal(h == 1 ? "true" : "false");
  if (h >= 3 && h <= 6) return Literal(r.Int(1, size_t(1) << (h - 3)).ToIntegerString());
  if (h == 7 || h == 14) {
    const double n = h == 7 ? std::bit_cast<double>(r.UInt(1, 8))
                            : static_cast<double>(std::bit_cast<float>(static_cast<uint32_t>(r.UInt(1, 4))));
    if (!std::isfinite(n)) return String(std::isnan(n) ? "NaN" : n < 0 ? "-Infinity" : "Infinity");
    char buffer[64];
    const auto result = std::to_chars(buffer, buffer + sizeof(buffer), n);
    return Literal(std::string(buffer, result.ptr));
  }
  if (h >= 8 && h <= 10) {
    const auto scale = r.UInt(1, 1);
    if (scale > 38) throw std::runtime_error("invalid decimal scale");
    return Literal(r.Int(2, size_t(1) << (h - 6)).ToString(static_cast<int32_t>(scale)));
  }
  if (h == 11) {
    const auto days = std::bit_cast<int64_t>(r.Int(1, 4).low_bits());
    if (days < -10000000 || days > 10000000) throw std::runtime_error("date out of range");
    const std::chrono::year_month_day date(std::chrono::sys_days{std::chrono::days(days)});
    return String(
        std::format("{:04}-{:02}-{:02}", int(date.year()), unsigned(date.month()), unsigned(date.day())));
  }
  if (h == 12 || h == 13 || h == 18 || h == 19)
    return String(Timestamp(std::bit_cast<int64_t>(r.Int(1, 8).low_bits()), h >= 18, h == 12 || h == 18));
  if (h == 15 || h == 16 || h == 20) {
    const size_t size = h == 20 ? 16 : r.UInt(1, 4), at = h == 20 ? 1 : 5;
    r.Check(at, size);
    if (h == 16) return String(r.Text(at, size));
    std::string hex;
    constexpr char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) {
      const auto byte = static_cast<uint8_t>(bytes[at + i]);
      if (h == 20 && (i == 4 || i == 6 || i == 8 || i == 10)) hex += '-';
      hex += digits[byte >> 4];
      hex += digits[byte & 15];
    }
    return String((h == 15 ? "\\x" : "") + hex);
  }
  if (h == 17) {
    const auto n = std::bit_cast<int64_t>(r.Int(1, 8).low_bits());
    if (n < 0 || n >= 86400000000LL) throw std::runtime_error("time out of range");
    return String(Timestamp(n, false, false).substr(11));
  }
  throw std::runtime_error("unsupported primitive " + std::to_string(h));
}
}  // namespace

VariantValue DecodeVariant(std::string_view metadata, std::string_view value) {
  try {
    Reader m{metadata};
    const auto header = m.UInt(0, 1);
    if ((header & 15) != 1) throw std::runtime_error("unsupported metadata version");
    const size_t width = (header >> 6) + 1, count = m.UInt(1, width);
    const size_t base = 1 + width + (count + 1) * width;
    m.Check(0, base);
    if (m.UInt(1 + width, width) != 0) throw std::runtime_error("invalid dictionary offset");
    std::vector<std::string> dictionary;
    for (size_t i = 0; i < count; ++i) {
      const size_t start = m.UInt(1 + width + i * width, width),
                   end = m.UInt(1 + width + (i + 1) * width, width);
      if (end < start) throw std::runtime_error("invalid dictionary offset");
      dictionary.push_back(m.Text(base + start, end - start));
    }
    if (base + m.UInt(1 + width + count * width, width) != metadata.size())
      throw std::runtime_error("invalid dictionary length");
    return Read(value, dictionary, 0);
  } catch (const std::exception& error) {
    return String(std::string("[VARIANT: ") + error.what() + "]");
  }
}
}  // namespace hardcharger::client
