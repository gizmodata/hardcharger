// Runtime metrics (Enterprise feature metrics), under GizmoSQL 1.x's names so
// existing dashboards keep working: counters and a duration histogram that the
// statement path updates, and gauges read when a scrape or a collection asks.
#pragma once

#include <duckdb_v2.h>

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

enum class StatementKind { kRead, kWrite, kDdl, kOther };
std::string_view StatementKindName(StatementKind kind);
StatementKind ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE type);

// The error class a failed statement counts under, from DuckDB's message prefix:
// out_of_memory, io, catalog, syntax, permission, cancelled, or other.
std::string_view ClassifyStatementError(std::string_view message);

using Labels = std::map<std::string, std::string>;

struct Sample {
  std::string name;
  Labels labels;
  double value = 0;
};

class Metrics {
 public:
  Metrics();

  bool enabled() const { return enabled_; }
  void Enable() { enabled_ = true; }

  // One finished client statement: status ok, error or cancelled.
  void RecordStatement(StatementKind kind, std::string_view status, std::string_view error_class,
                       double seconds);
  void CountSessionOpened();
  void CountSessionReaped();
  void CountQueueRejected();
  void CountQueueBypass();
  void ObserveQueueWait(double seconds);

  // Gauges that are computed, not counted: called on every snapshot, appending samples.
  void AddCollector(std::function<void(std::vector<Sample>&)> collector);

  // Every sample, counters first, then collected gauges.
  std::vector<Sample> Snapshot() const;
  // Prometheus text format (0.0.4): one HELP and TYPE per family, escaped labels.
  std::string Render() const;

 private:
  struct Histogram {
    std::vector<double> bounds;
    std::vector<uint64_t> counts;  // per bound, non-cumulative; the last is +Inf
    double sum = 0;
    uint64_t count = 0;
    void Observe(double value);
  };

  std::atomic<bool> enabled_ = false;
  mutable std::mutex mutex_;
  std::map<std::pair<std::string, Labels>, double> counters_;
  std::map<Labels, Histogram> durations_;  // by kind
  Histogram queue_wait_;
  std::vector<std::function<void(std::vector<Sample>&)>> collectors_;
};

// HELP text and TYPE of a family, for rendering ("" if unknown).
struct FamilyInfo {
  std::string_view type;
  std::string_view help;
};
FamilyInfo Family(std::string_view name);

}  // namespace hardcharger

namespace hardcharger {

// GET /metrics on its own plaintext port, as GizmoSQL 1.x serves it (Prometheus
// scrapers and the GizmoData Cloud console read it there). Everything else is 404.
class MetricsEndpoint {
 public:
  MetricsEndpoint(const Metrics& metrics, std::string address, uint16_t port);
  ~MetricsEndpoint();
  void Start();
  void Stop();
  uint16_t port() const { return port_; }

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
  uint16_t port_;
};

}  // namespace hardcharger
