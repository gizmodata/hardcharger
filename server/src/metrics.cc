#include "metrics.hpp"

#include <cmath>
#include <format>
#include <set>

namespace hardcharger {

namespace {

const std::vector<double>& DurationBounds() {
  static const std::vector<double> bounds = {0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
                                             1,     2.5,  5,     10,   30,  60,   300};
  return bounds;
}

std::string Escape(std::string_view text, bool label) {
  std::string out;
  for (char c : text) {
    if (c == '\\') out += "\\\\";
    else if (c == '\n') out += "\\n";
    else if (c == '"' && label) out += "\\\"";
    else out += c;
  }
  return out;
}

std::string FormatValue(double value) {
  if (std::isnan(value)) return "NaN";
  if (std::isinf(value)) return value > 0 ? "+Inf" : "-Inf";
  if (value == std::floor(value) && std::fabs(value) < 1e15) return std::format("{}", static_cast<int64_t>(value));
  return std::format("{}", value);
}

std::string FamilyOf(std::string_view name) {
  for (std::string_view suffix : {"_bucket", "_count", "_sum"}) {
    if (name.ends_with(suffix)) {
      const std::string base(name.substr(0, name.size() - suffix.size()));
      if (Family(base).type == "histogram") return base;
    }
  }
  return std::string(name);
}

}  // namespace

std::string_view StatementKindName(StatementKind kind) {
  switch (kind) {
    case StatementKind::kRead: return "read";
    case StatementKind::kWrite: return "write";
    case StatementKind::kDdl: return "ddl";
    case StatementKind::kOther: return "other";
  }
  return "other";
}

StatementKind ClassifyStatement(DUCKDB_V2_STATEMENT_TYPE type) {
  switch (type) {
    case DUCKDB_V2_STATEMENT_TYPE_SELECT:
    case DUCKDB_V2_STATEMENT_TYPE_EXPLAIN:
    case DUCKDB_V2_STATEMENT_TYPE_RELATION:
      return StatementKind::kRead;
    case DUCKDB_V2_STATEMENT_TYPE_INSERT:
    case DUCKDB_V2_STATEMENT_TYPE_UPDATE:
    case DUCKDB_V2_STATEMENT_TYPE_DELETE:
    case DUCKDB_V2_STATEMENT_TYPE_MERGE_INTO:
    case DUCKDB_V2_STATEMENT_TYPE_COPY:
    case DUCKDB_V2_STATEMENT_TYPE_COPY_DATABASE:
      return StatementKind::kWrite;
    case DUCKDB_V2_STATEMENT_TYPE_CREATE:
    case DUCKDB_V2_STATEMENT_TYPE_CREATE_FUNC:
    case DUCKDB_V2_STATEMENT_TYPE_ALTER:
    case DUCKDB_V2_STATEMENT_TYPE_DROP:
    case DUCKDB_V2_STATEMENT_TYPE_ATTACH:
    case DUCKDB_V2_STATEMENT_TYPE_DETACH:
      return StatementKind::kDdl;
    default:
      return StatementKind::kOther;
  }
}

std::string_view ClassifyStatementError(std::string_view message) {
  if (message.starts_with("Out of Memory Error")) return "out_of_memory";
  if (message.starts_with("IO Error")) return "io";
  if (message.starts_with("Catalog Error")) return "catalog";
  if (message.starts_with("Parser Error")) return "syntax";
  if (message.starts_with("Permission denied") || message.starts_with("Permission Error")) return "permission";
  if (message.starts_with("INTERRUPT Error")) return "cancelled";
  return "other";
}

FamilyInfo Family(std::string_view name) {
  static const std::map<std::string_view, FamilyInfo> kFamilies = {
      {"hardcharger_statements_total", {"counter", "Client statements finished, by status."}},
      {"hardcharger_statement_errors_total", {"counter", "Failed client statements, by error class."}},
      {"hardcharger_statement_duration_seconds", {"histogram", "Client statement run time, by kind."}},
      {"hardcharger_sessions_opened_total", {"counter", "Client sessions opened."}},
      {"hardcharger_sessions_reaped_total", {"counter", "Client sessions ended for being idle or killed."}},
      {"hardcharger_queue_rejected_total", {"counter", "Statements the queue refused or timed out."}},
      {"hardcharger_queue_admin_bypass_total", {"counter", "Statements that skipped the queue."}},
      {"hardcharger_queue_wait_seconds", {"histogram", "Time statements waited in the queue."}},
      {"hardcharger_build_info", {"gauge", "Server build: version, edition, DuckDB version."}},
      {"hardcharger_up_seconds", {"gauge", "Seconds since the server started."}},
      {"hardcharger_sessions", {"gauge", "Open client sessions, by state."}},
      {"hardcharger_session_limit", {"gauge", "max_sessions (0: unlimited)."}},
      {"hardcharger_statements_active", {"gauge", "Statements running now."}},
      {"hardcharger_concurrency_limit", {"gauge", "max_concurrent_statements (0: no queue)."}},
      {"hardcharger_queue_depth", {"gauge", "Statements holding or waiting for a queue slot."}},
      {"hardcharger_draining", {"gauge", "1 while the server shuts down."}},
      {"hardcharger_health_check_status", {"gauge", "1 when the health check passes."}},
      {"hardcharger_metrics_collection_success", {"gauge", "1 when the last collection succeeded."}},
      {"hardcharger_metrics_last_collection_success_seconds",
       {"gauge", "Unix time of the last successful collection."}},
      {"hardcharger_duckdb_memory_limit_bytes", {"gauge", "DuckDB memory_limit in bytes."}},
      {"hardcharger_duckdb_memory_usage_bytes", {"gauge", "Memory DuckDB's buffer manager holds."}},
      {"process_resident_memory_bytes", {"gauge", "Resident memory of the server process."}},
      {"hardcharger_last_exit_unclean", {"gauge", "1 if the last run on this database did not stop cleanly (NaN: unknown)."}},
      {"hardcharger_unclean_exits_total", {"counter", "Runs on this database that did not stop cleanly."}},
  };
  auto it = kFamilies.find(name);
  return it == kFamilies.end() ? FamilyInfo{"gauge", ""} : it->second;
}

void Metrics::Histogram::Observe(double value) {
  if (counts.empty()) counts.assign(bounds.size() + 1, 0);
  std::size_t i = 0;
  while (i < bounds.size() && value > bounds[i]) i++;
  counts[i]++;
  sum += value;
  count++;
}

Metrics::Metrics() { queue_wait_.bounds = DurationBounds(); }

void Metrics::RecordStatement(StatementKind kind, std::string_view status, std::string_view error_class,
                              double seconds) {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  counters_[{"hardcharger_statements_total", {{"status", std::string(status)}}}] += 1;
  if (status == "error") {
    counters_[{"hardcharger_statement_errors_total", {{"class", std::string(error_class)}}}] += 1;
  }
  auto& histogram = durations_[{{"kind", std::string(StatementKindName(kind))}}];
  if (histogram.bounds.empty()) histogram.bounds = DurationBounds();
  histogram.Observe(seconds);
}

void Metrics::CountSessionOpened() {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  counters_[{"hardcharger_sessions_opened_total", {}}] += 1;
}

void Metrics::CountSessionReaped() {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  counters_[{"hardcharger_sessions_reaped_total", {}}] += 1;
}

void Metrics::CountQueueRejected() {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  counters_[{"hardcharger_queue_rejected_total", {}}] += 1;
}

void Metrics::CountQueueBypass() {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  counters_[{"hardcharger_queue_admin_bypass_total", {}}] += 1;
}

void Metrics::ObserveQueueWait(double seconds) {
  if (!enabled_) return;
  std::lock_guard lock(mutex_);
  queue_wait_.Observe(seconds);
}

void Metrics::AddCollector(std::function<void(std::vector<Sample>&)> collector) {
  std::lock_guard lock(mutex_);
  collectors_.push_back(std::move(collector));
}

std::vector<Sample> Metrics::Snapshot() const {
  std::vector<Sample> samples;
  std::vector<std::function<void(std::vector<Sample>&)>> collectors;
  {
    std::lock_guard lock(mutex_);
    // Families that start at zero, so a scrape shows them before the first event.
    for (const char* status : {"ok", "error", "cancelled"}) {
      auto key = std::make_pair(std::string("hardcharger_statements_total"), Labels{{"status", status}});
      samples.push_back({key.first, key.second, counters_.contains(key) ? counters_.at(key) : 0});
    }
    for (const auto& [key, value] : counters_) {
      if (key.first == "hardcharger_statements_total") continue;
      samples.push_back({key.first, key.second, value});
    }
    for (const char* name : {"hardcharger_sessions_opened_total", "hardcharger_sessions_reaped_total",
                             "hardcharger_queue_rejected_total", "hardcharger_queue_admin_bypass_total"}) {
      if (!counters_.contains({name, {}})) samples.push_back({name, {}, 0});
    }
    auto histogram = [&](const std::string& name, const Labels& labels, const Histogram& h) {
      uint64_t cumulative = 0;
      for (std::size_t i = 0; i <= h.bounds.size(); i++) {
        cumulative += h.counts.empty() ? 0 : h.counts[i];
        Labels with = labels;
        with["le"] = i < h.bounds.size() ? FormatValue(h.bounds[i]) : "+Inf";
        samples.push_back({name + "_bucket", with, static_cast<double>(cumulative)});
      }
      samples.push_back({name + "_sum", labels, h.sum});
      samples.push_back({name + "_count", labels, static_cast<double>(h.count)});
    };
    for (const auto& [labels, h] : durations_) histogram("hardcharger_statement_duration_seconds", labels, h);
    histogram("hardcharger_queue_wait_seconds", {}, queue_wait_);
    collectors = collectors_;
  }
  for (const auto& collect : collectors) collect(samples);
  return samples;
}

std::string Metrics::Render() const {
  const auto samples = Snapshot();
  std::string out;
  std::set<std::string> described;
  // Group samples by family, keeping the first appearance order.
  std::vector<std::string> order;
  std::map<std::string, std::vector<const Sample*>> by_family;
  for (const auto& sample : samples) {
    const std::string family = FamilyOf(sample.name);
    if (!by_family.contains(family)) order.push_back(family);
    by_family[family].push_back(&sample);
  }
  for (const auto& family : order) {
    const FamilyInfo info = Family(family);
    if (!info.help.empty()) out += "# HELP " + family + " " + Escape(info.help, false) + "\n";
    out += "# TYPE " + family + " " + std::string(info.type) + "\n";
    for (const Sample* sample : by_family[family]) {
      out += sample->name;
      if (!sample->labels.empty()) {
        out += "{";
        bool first = true;
        for (const auto& [key, value] : sample->labels) {
          out += (first ? "" : ",") + key + "=\"" + Escape(value, true) + "\"";
          first = false;
        }
        out += "}";
      }
      out += " " + FormatValue(sample->value) + "\n";
    }
  }
  return out;
}

}  // namespace hardcharger

#include <boost/asio.hpp>
#include <boost/beast.hpp>

#include <thread>

namespace hardcharger {

namespace {
namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = asio::ip::tcp;
}  // namespace

class MetricsEndpoint::Impl {
 public:
  Impl(const Metrics& metrics, const std::string& address, uint16_t port)
      : metrics_(metrics), acceptor_(io_, tcp::endpoint(asio::ip::make_address(address), port)) {}

  uint16_t port() const { return acceptor_.local_endpoint().port(); }

  void Start() {
    Accept();
    thread_ = std::thread([this] { io_.run(); });
  }

  void Stop() {
    io_.stop();
    if (thread_.joinable()) thread_.join();
  }

 private:
  void Accept() {
    acceptor_.async_accept([this](boost::system::error_code ec, tcp::socket socket) {
      if (ec) return;
      Serve(std::move(socket));
      Accept();
    });
  }

  // One request per connection: scrapes are rare and small.
  void Serve(tcp::socket socket) {
    boost::system::error_code ec;
    beast::flat_buffer buffer;
    http::request<http::string_body> request;
    socket.set_option(asio::socket_base::linger(true, 1), ec);
    http::read(socket, buffer, request, ec);
    if (ec) return;
    http::response<http::string_body> response;
    response.version(request.version());
    response.keep_alive(false);
    const std::string_view target(request.target().data(), request.target().size());
    if (request.method() == http::verb::get && (target == "/metrics" || target.starts_with("/metrics?"))) {
      response.result(http::status::ok);
      response.set(http::field::content_type, "text/plain; version=0.0.4; charset=utf-8");
      response.body() = metrics_.Render();
    } else {
      response.result(http::status::not_found);
      response.set(http::field::content_type, "text/plain; charset=utf-8");
      response.body() = "not found: this port serves /metrics\n";
    }
    response.prepare_payload();
    http::write(socket, response, ec);
    socket.shutdown(tcp::socket::shutdown_both, ec);
  }

  const Metrics& metrics_;
  asio::io_context io_;
  tcp::acceptor acceptor_;
  std::thread thread_;
};

MetricsEndpoint::MetricsEndpoint(const Metrics& metrics, std::string address, uint16_t port)
    : impl_(std::make_unique<Impl>(metrics, address, port)), port_(impl_->port()) {}

MetricsEndpoint::~MetricsEndpoint() { Stop(); }

void MetricsEndpoint::Start() { impl_->Start(); }

void MetricsEndpoint::Stop() {
  if (impl_) impl_->Stop();
}

}  // namespace hardcharger
