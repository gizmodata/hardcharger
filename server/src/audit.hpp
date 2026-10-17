// Tamper-evident access-control audit records (as GizmoSQL 1.x writes them).
//
// Every audit record joins a hash chain: it carries the chain's id, its sequence
// number, the previous record's digest and its own digest over its fields and message.
// Editing, removing, reordering or inserting a record breaks the chain, which
// `hardcharger --verify-audit-log FILE` reports. With audit_chain_key set, digests are
// HMAC-SHA256 under that key, so nobody without it can rebuild a consistent chain after
// editing; without it they are plain SHA-256 and show only accidental or partial
// changes.
//
// A server starts a new chain (a new id, sequence 1) when access control starts. The
// newest records of a chain can be cut off without a trace in the file itself: ship the
// log elsewhere as it is written to catch that.
#pragma once

#include <cstdint>
#include <istream>
#include <string>
#include <string_view>
#include <vector>

#include "logging.hpp"

namespace hardcharger {

// Starts a new chain (an empty key: SHA-256) and writes its first record.
void StartAuditChain(const std::string& key);

// Writes an audit record: the chain fields are added and it is logged. Nothing is
// written, and the chain does not advance, when the level is below the log level.
void Audit(LogLevel level, std::string_view message, LogFields fields);

struct AuditChainReport {
  int64_t records = 0;              // chained records found
  std::vector<std::string> chains;  // "id: sequence 1 to N"
  std::vector<std::string> problems;
  bool ok() const { return records > 0 && problems.empty(); }
};

// Checks the chained records in a JSON-format log (log_format json); other lines are
// skipped. `key`: the audit_chain_key of the server that wrote it (empty for an unkeyed
// chain). `partial`: the log may start inside a chain (a rotated log).
AuditChainReport VerifyAuditLog(std::istream& in, const std::string& key, bool partial = false);

}  // namespace hardcharger
