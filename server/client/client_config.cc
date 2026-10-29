// Ported from GizmoSQL 1.x src/client/client_config.cpp.

#include "client_config.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <iostream>
#include <map>
#include <sstream>
#include <vector>

namespace hardcharger::client {

std::string OutputModeToString(OutputMode mode) {
  switch (mode) {
    case OutputMode::TABLE:     return "table";
    case OutputMode::BOX:       return "box";
    case OutputMode::CSV:       return "csv";
    case OutputMode::TABS:      return "tabs";
    case OutputMode::JSON:      return "json";
    case OutputMode::JSONLINES: return "jsonlines";
    case OutputMode::MARKDOWN:  return "markdown";
    case OutputMode::LINE:      return "line";
    case OutputMode::LIST:      return "list";
    case OutputMode::HTML:      return "html";
    case OutputMode::LATEX:     return "latex";
    case OutputMode::INSERT:    return "insert";
    case OutputMode::QUOTE:     return "quote";
    case OutputMode::ASCII:     return "ascii";
    case OutputMode::TRASH:     return "trash";
  }
  return "unknown";
}

std::optional<OutputMode> OutputModeFromString(const std::string& name) {
  std::string lower = name;
  std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);

  if (lower == "table")     return OutputMode::TABLE;
  if (lower == "box")       return OutputMode::BOX;
  if (lower == "csv")       return OutputMode::CSV;
  if (lower == "tabs")      return OutputMode::TABS;
  if (lower == "json")      return OutputMode::JSON;
  if (lower == "jsonlines") return OutputMode::JSONLINES;
  if (lower == "markdown")  return OutputMode::MARKDOWN;
  if (lower == "line")      return OutputMode::LINE;
  if (lower == "list")      return OutputMode::LIST;
  if (lower == "html")      return OutputMode::HTML;
  if (lower == "latex")     return OutputMode::LATEX;
  if (lower == "insert")    return OutputMode::INSERT;
  if (lower == "quote")     return OutputMode::QUOTE;
  if (lower == "ascii")     return OutputMode::ASCII;
  if (lower == "trash")     return OutputMode::TRASH;
  return std::nullopt;
}

void ResolveEnvironmentVariables(ClientConfig& config) {
  auto get_env = [](const char* name) -> const char* {
    const char* val = std::getenv(name);
    return (val != nullptr && val[0] != '\0') ? val : nullptr;
  };

  // A URI names the host, port and TLS itself: the environment never changes them.
  if (config.host == "localhost" && !config.uri_given) {
    if (const char* val = get_env("HARDCHARGER_HOST")) {
      config.host = val;
    }
  }

  if (config.port == 31337 && !config.uri_given) {
    if (const char* val = get_env("HARDCHARGER_PORT")) {
      try {
        config.port = std::stoi(val);
      } catch (...) {
        // ignore invalid port
      }
    }
  }

  if (config.username.empty()) {
    // HARDCHARGER_USER is the client's variable. The server names its administrator
    // HARDCHARGER_ADMIN_USERNAME; accept that as a fallback so one setup (a container's
    // environment) serves both. The password has no such fallback: HARDCHARGER_PASSWORD
    // is the user's own credential, never the server's administrator password.
    if (const char* val = get_env("HARDCHARGER_USER")) {
      config.username = val;
    } else if (const char* val = get_env("HARDCHARGER_ADMIN_USERNAME")) {
      config.username = val;
    }
  }

  if (!config.password_provided) {
    if (const char* val = get_env("HARDCHARGER_PASSWORD")) {
      config.password = val;
      config.password_provided = true;
    }
  }

  if (!config.tls_given && !config.uri_given) {
    if (const char* val = get_env("HARDCHARGER_TLS")) {
      std::string lower = val;
      std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
      if (lower == "true" || lower == "1") {
        config.use_tls = true;
      } else if (lower == "false" || lower == "0") {
        config.use_tls = false;
      } else {
        // An unreadable value keeps TLS on: it never turns encryption off by accident.
        std::cerr << "Warning: HARDCHARGER_TLS='" << val << "' is not true or false; TLS stays on" << std::endl;
        config.use_tls = true;
      }
    }
  }

  if (config.tls_roots.empty()) {
    if (const char* val = get_env("HARDCHARGER_TLS_ROOTS")) {
      config.tls_roots = val;
    }
  }

  if (config.session_tag.empty()) {
    if (const char* val = get_env("HARDCHARGER_SESSION_TAG")) {
      config.session_tag = val;
    }
  }

  if (config.query_tag.empty()) {
    if (const char* val = get_env("HARDCHARGER_QUERY_TAG")) {
      config.query_tag = val;
    }
  }

  if (config.result_partitions == 0) {
    if (const char* val = get_env("HARDCHARGER_RESULT_PARTITIONS")) {
      try {
        config.result_partitions = std::stoi(val);
      } catch (const std::exception&) {
        config.result_partitions = 0;
      }
    }
  }
  if (config.result_partitions == 0) config.result_partitions = 8;  // the default: split large results
  config.result_partitions = std::clamp(config.result_partitions, 1, 256);
}

std::string HomeDirectory() {
#ifdef _WIN32
  const char* home = std::getenv("USERPROFILE");
#else
  const char* home = std::getenv("HOME");
#endif
  return home != nullptr ? std::string(home) : std::string();
}

namespace {

std::string Lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });
  return text;
}

bool UriError(const std::string& message) {
  std::cerr << "Error: " << message << std::endl;
  return false;
}

// The URI without its user info (which may hold a password), for messages.
std::string Redacted(const std::string& uri) {
  const auto start = uri.find("://");
  if (start == std::string::npos) return uri;
  const auto authority_end = uri.find_first_of("/?#", start + 3);
  const auto at = uri.rfind('@', authority_end == std::string::npos ? std::string::npos : authority_end);
  if (at == std::string::npos || at < start + 3) return uri;
  return uri.substr(0, start + 3) + "***" + uri.substr(at);
}

// What the parameters the client's URIs used to take are now (a hint, not an alias).
std::string FormerParameterHint(const std::string& name) {
  if (name == "username") return "--username";
  if (name == "useEncryption") return "TLS is on unless the URI says ?tls=false";
  if (name == "disableCertificateVerification") return "--tls-skip-verify";
  if (name == "tlsRoots") return "--tls-roots";
  if (name == "authType") return "--auth-type";
  return "";
}

}  // namespace

// The URIs of the Hardcharger ADBC driver (hardcharger-adbc ToChargewireURI, then chargewire-adbc
// parseURI), exactly:
//   hardcharger://host[:port][/prefix][?tls=false]  HTTPS unless ?tls=false; port 31337
//   chargewire://host[:port][/prefix][?tls=false]         the same, port 443
//   https://host[:port][/prefix], http://...        TLS, or plain HTTP; ports 443 and 80
// tls takes true/false/1/0 (any case); no other parameter, no user info, and a host are
// required. The base path is /chargewire unless the URI names one.
bool ParseConnectionURI(const std::string& uri, ClientConfig& config) {
  const auto scheme_end = uri.find("://");
  if (scheme_end == std::string::npos || scheme_end == 0) {
    return UriError("invalid uri " + Redacted(uri) + ": it needs a scheme, e.g. hardcharger://host:31337");
  }
  const std::string scheme = Lower(uri.substr(0, scheme_end));
  bool tls = true;
  bool tls_parameter = false;
  int default_port = 443;
  if (scheme == "hardcharger") {
    tls_parameter = true;
    default_port = 31337;
  } else if (scheme == "chargewire") {
    tls_parameter = true;
  } else if (scheme == "https") {
  } else if (scheme == "http") {
    tls = false;
    default_port = 80;
  } else if (scheme.rfind("hardcharger+", 0) == 0) {
    return UriError(uri.substr(0, scheme_end) +
                    ":// is not a Hardcharger URI scheme: use hardcharger://host:port for HTTPS, or "
                    "hardcharger://host:port?tls=false for plain HTTP");
  } else {
    return UriError("invalid uri " + Redacted(uri) + ": the scheme must be hardcharger://, chargewire://, https:// or http://");
  }

  std::string rest = uri.substr(scheme_end + 3);
  if (const auto hash = rest.find('#'); hash != std::string::npos) rest.resize(hash);
  std::string query;
  if (const auto q = rest.find('?'); q != std::string::npos) {
    query = rest.substr(q + 1);
    rest.resize(q);
  }
  std::string path;
  if (const auto slash = rest.find('/'); slash != std::string::npos) {
    path = rest.substr(slash);
    rest.resize(slash);
  }
  const std::string& authority = rest;
  if (authority.find('@') != std::string::npos) {
    return UriError("invalid uri " + Redacted(uri) +
                    ": credentials do not belong in the uri; use --username and HARDCHARGER_PASSWORD (or -W)");
  }

  std::string host;
  std::string port_text;
  if (!authority.empty() && authority.front() == '[') {
    const auto close = authority.find(']');
    if (close == std::string::npos) return UriError("invalid uri " + Redacted(uri) + ": unclosed [ in the host");
    host = authority.substr(1, close - 1);
    const std::string after = authority.substr(close + 1);
    if (!after.empty()) {
      if (after.front() != ':') return UriError("invalid uri " + Redacted(uri) + ": invalid host");
      port_text = after.substr(1);
    }
  } else {
    const auto colon = authority.find(':');
    host = authority.substr(0, colon);
    if (colon != std::string::npos) port_text = authority.substr(colon + 1);
  }
  if (host.empty()) return UriError("invalid uri " + Redacted(uri) + ": no host");
  int port = default_port;
  if (!port_text.empty()) {
    const bool digits = port_text.size() <= 5 &&
                        std::all_of(port_text.begin(), port_text.end(), [](unsigned char c) { return std::isdigit(c); });
    port = digits ? std::stoi(port_text) : -1;
    if (port < 1 || port > 65535) {
      return UriError("invalid uri " + Redacted(uri) + ": invalid port \"" + port_text + "\"");
    }
  }

  std::vector<std::string> unknown;
  std::istringstream pairs(query);
  std::string pair;
  bool tls_seen = false;
  while (std::getline(pairs, pair, '&')) {
    if (pair.empty()) continue;
    const auto eq = pair.find('=');
    const std::string key = pair.substr(0, eq);
    const std::string value = eq == std::string::npos ? "" : pair.substr(eq + 1);
    if (key == "tls" && tls_parameter) {
      if (tls_seen) continue;  // the first one counts, as in Go's url.Values.Get
      tls_seen = true;
      const std::string v = Lower(value);
      if (v == "true" || v == "1") {
        tls = true;
      } else if (v == "false" || v == "0") {
        tls = false;
      } else if (!v.empty()) {
        return UriError("invalid value \"" + value + "\" for uri parameter tls: want \"true\" or \"false\"");
      }
    } else if (std::find(unknown.begin(), unknown.end(), key) == unknown.end()) {
      unknown.push_back(key);
    }
  }
  if (!unknown.empty()) {
    std::sort(unknown.begin(), unknown.end());
    std::string names;
    std::string hints;
    for (const auto& key : unknown) {
      names += (names.empty() ? "" : ", ") + key;
      if (const auto hint = FormerParameterHint(key); !hint.empty()) {
        hints += (hints.empty() ? "" : "; ") + key + ": " + hint;
      }
    }
    return UriError("invalid uri " + Redacted(uri) + ": unknown parameter(s) " + names +
                    (hints.empty() ? "" : " (" + hints + ")"));
  }

  while (!path.empty() && path.back() == '/') path.pop_back();
  config.host = host;
  config.port = port;
  config.use_tls = tls;
  config.base_path = path.empty() ? "/chargewire" : path;
  config.uri_given = true;
  return true;
}

// The URI of the current connection, in the form ParseConnectionURI reads (no credentials).
std::string BuildConnectionURI(const ClientConfig& config) {
  const bool v6 = config.host.find(':') != std::string::npos;
  std::string uri = "hardcharger://" + (v6 ? "[" + config.host + "]" : config.host) + ":" + std::to_string(config.port);
  if (config.base_path != "/chargewire") uri += config.base_path;
  if (!config.use_tls) uri += "?tls=false";
  return uri;
}

}  // namespace hardcharger::client
