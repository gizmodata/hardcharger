#include "audit.hpp"

#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>

#include <boost/json.hpp>
#include <chrono>
#include <map>
#include <mutex>
#include <sstream>

namespace hardcharger {

namespace {

constexpr const char* kChain = "audit_chain";
constexpr const char* kSeq = "audit_seq";
constexpr const char* kPrev = "audit_prev";
constexpr const char* kHash = "audit_hash";
constexpr const char* kKeys = "audit_keys";
constexpr const char* kAlg = "audit_alg";
const std::string kGenesis(64, '0');

std::string Hex(const unsigned char* bytes, std::size_t n) {
  static constexpr char kDigits[] = "0123456789abcdef";
  std::string out;
  out.reserve(n * 2);
  for (std::size_t i = 0; i < n; ++i) {
    out.push_back(kDigits[bytes[i] >> 4]);
    out.push_back(kDigits[bytes[i] & 0xf]);
  }
  return out;
}

std::string Digest(const std::string& key, const std::string& data) {
  unsigned char out[EVP_MAX_MD_SIZE];
  unsigned int length = 0;
  if (key.empty()) {
    EVP_Digest(data.data(), data.size(), out, &length, EVP_sha256(), nullptr);
  } else {
    HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
         reinterpret_cast<const unsigned char*>(data.data()), data.size(), out, &length);
  }
  return Hex(out, length);
}

// What a record's digest covers: the previous digest, the message, and each covered
// field as length-prefixed key and value, in key order.
std::string Canonical(const std::string& prev, std::string_view message,
                      const std::map<std::string, std::string>& fields) {
  std::string out = prev;
  out += '\n';
  out += std::to_string(message.size());
  out += ':';
  out.append(message.data(), message.size());
  out += '\n';
  for (const auto& [key, value] : fields) {
    out +=
        std::to_string(key.size()) + ':' + key + std::to_string(value.size()) + ':' + value + '\n';
  }
  return out;
}

struct ChainState {
  std::mutex mutex;
  std::string id;
  std::string key;
  int64_t seq = 0;
  std::string prev = kGenesis;
};

ChainState& Chain() {
  static ChainState state;
  return state;
}

std::string NewChainId() {
  unsigned char bytes[8];
  if (RAND_bytes(bytes, sizeof(bytes)) != 1) {
    const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
    for (std::size_t i = 0; i < sizeof(bytes); ++i)
      bytes[i] = static_cast<unsigned char>(now >> (8 * i));
  }
  return Hex(bytes, sizeof(bytes));
}

// Adds the chain fields to `fields`, and its digest.
void Seal(LogFields& fields, std::string_view message) {
  auto& chain = Chain();
  std::lock_guard lock(chain.mutex);
  if (chain.id.empty()) chain.id = NewChainId();
  fields.emplace_back(kChain, chain.id);
  fields.emplace_back(kSeq, std::to_string(++chain.seq));
  fields.emplace_back(kPrev, chain.prev);
  fields.emplace_back(kAlg, chain.key.empty() ? "sha256" : "hmac-sha256");
  // The JSON log keeps the last value of a repeated key.
  std::map<std::string, std::string> covered;
  for (const auto& [key, value] : fields) covered[key] = value;
  std::string keys;
  for (const auto& [key, value] : covered) keys += (keys.empty() ? "" : ",") + key;
  covered[kKeys] = keys + "," + kKeys;
  fields.emplace_back(kKeys, covered[kKeys]);
  chain.prev = Digest(chain.key, Canonical(chain.prev, message, covered));
  fields.emplace_back(kHash, chain.prev);
}

std::vector<std::string> Split(const std::string& text, char separator) {
  std::vector<std::string> out;
  std::string part;
  std::istringstream in(text);
  while (std::getline(in, part, separator)) out.push_back(part);
  return out;
}

std::string Text(const boost::json::value& value) {
  if (value.is_string()) return std::string(value.as_string());
  if (value.is_bool()) return value.as_bool() ? "true" : "false";
  if (value.is_int64()) return std::to_string(value.as_int64());
  return boost::json::serialize(value);
}

std::string Field(const boost::json::object& record, const char* key) {
  const auto* value = record.if_contains(key);
  return value ? Text(*value) : std::string();
}

}  // namespace

void StartAuditChain(const std::string& key) {
  {
    auto& chain = Chain();
    std::lock_guard lock(chain.mutex);
    chain.id = NewChainId();
    chain.key = key;
    chain.seq = 0;
    chain.prev = kGenesis;
  }
  Audit(LogLevel::kInfo, "access-control audit chain started",
        {{"kind", "access_control"}, {"keyed", key.empty() ? "false" : "true"}});
}

void Audit(LogLevel level, std::string_view message, LogFields fields) {
  if (!LogEnabled(level)) return;
  Seal(fields, message);
  Log(level, message, fields);
}

AuditChainReport VerifyAuditLog(std::istream& in, const std::string& key, bool partial) {
  AuditChainReport report;
  // chain id -> sequence -> (line, record)
  std::map<std::string, std::map<int64_t, std::pair<int64_t, boost::json::object>>> by_chain;
  std::string text;
  int64_t line_number = 0;
  while (std::getline(in, text)) {
    ++line_number;
    if (text.empty() || text[0] != '{') continue;
    boost::system::error_code ec;
    auto parsed = boost::json::parse(text, ec);
    if (ec || !parsed.is_object() || !parsed.as_object().contains(kHash)) continue;
    auto& record = parsed.as_object();
    const std::string where = "line " + std::to_string(line_number);
    int64_t seq = 0;
    try {
      seq = std::stoll(Field(record, kSeq));
    } catch (const std::exception&) {
      seq = 0;
    }
    if (Field(record, kChain).empty() || seq <= 0) {
      report.problems.push_back(where + ": an audit record without its chain id or sequence");
      continue;
    }
    auto& records = by_chain[Field(record, kChain)];
    if (records.contains(seq)) {
      report.problems.push_back(where + ": chain " + Field(record, kChain) + " has sequence " +
                                std::to_string(seq) + " twice (also line " +
                                std::to_string(records[seq].first) + ")");
      continue;
    }
    records[seq] = {line_number, std::move(record)};
  }
  for (auto& [chain, records] : by_chain) {
    std::string prev;
    int64_t expected = records.begin()->first;
    if (expected != 1 && !partial) {
      report.problems.push_back("chain " + chain + " starts at sequence " +
                                std::to_string(expected) + ": its first records are missing");
    }
    for (auto& [seq, entry] : records) {
      auto& [line, record] = entry;
      const std::string where = "line " + std::to_string(line) + " (chain " + chain +
                                ", sequence " + std::to_string(seq) + ")";
      ++report.records;
      if (seq != expected) {
        report.problems.push_back(where + ": sequence " + std::to_string(expected) +
                                  (seq - expected > 1 ? " to " + std::to_string(seq - 1) : "") +
                                  " is missing");
      }
      expected = seq + 1;
      const std::string claimed_prev = Field(record, kPrev);
      if (!prev.empty() && claimed_prev != prev) {
        report.problems.push_back(where + ": does not follow the record before it");
      } else if (prev.empty() && seq == 1 && claimed_prev != kGenesis) {
        report.problems.push_back(where + ": the first record does not start the chain");
      }
      const std::string alg = Field(record, kAlg);
      if (!key.empty() && alg != "hmac-sha256") {
        report.problems.push_back(where + ": not sealed with the key");
      } else if (key.empty() && alg == "hmac-sha256") {
        report.problems.push_back(where + ": sealed with a key; pass audit_chain_key to check it");
      }
      std::map<std::string, std::string> covered;
      bool complete = true;
      for (const auto& name : Split(Field(record, kKeys), ',')) {
        if (!record.contains(name)) {
          report.problems.push_back(where + ": the field " + name + " is missing");
          complete = false;
          continue;
        }
        covered[name] = Text(record.at(name));
      }
      if (!covered.contains(kKeys) || !covered.contains(kChain) || !covered.contains(kSeq) ||
          !covered.contains(kPrev)) {
        report.problems.push_back(where + ": the record does not cover its chain fields");
        complete = false;
      }
      const std::string digest =
          Digest(key, Canonical(claimed_prev, Field(record, "message"), covered));
      if (complete && digest != Field(record, kHash)) {
        report.problems.push_back(where + ": was changed after it was written");
      }
      prev = Field(record, kHash);
    }
    report.chains.push_back(chain + ": sequence " + std::to_string(records.begin()->first) +
                            " to " + std::to_string(records.rbegin()->first));
  }
  return report;
}

}  // namespace hardcharger
