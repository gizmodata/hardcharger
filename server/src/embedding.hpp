// How main() talks to an app that runs Hardcharger inside its own process (the iOS app,
// ios/), through the C API in embedded/hardcharger_embedded.h. CMake builds main.cc twice:
// as hardcharger-server's main(), where these do nothing and stop requests are the
// process's signals, and with HARDCHARGER_EMBEDDED into the hardcharger_embedded library
// (main renamed hardcharger_server_main), where they report to the app and take its stop
// requests instead of signals: an app can't have SIGINT/SIGTERM delivered to one thread
// (warpsql::StopSignals::Wake() sends SIGTERM to the process, which would end the app).
#pragma once

#include <cstdint>
#include <string_view>

#ifndef HARDCHARGER_EMBEDDED
#include "warpsql/stop_signals.hpp"
#endif

namespace hardcharger::embedding {

#ifdef HARDCHARGER_EMBEDDED

// The server serves on `port` (the one it bound, so 0 in the configuration works).
void Ready(uint16_t port);
// The server failed to start or stopped on an error: the message the log has too.
void Failed(std::string_view message);

// StopSignals' Wait() and Wake(), for the app's hardcharger_server_request_stop(): each
// request ends one Wait(), as a signal would (two stop at once, as a second signal does).
class StopRequests {
 public:
  StopRequests() = default;
  StopRequests(const StopRequests&) = delete;
  StopRequests& operator=(const StopRequests&) = delete;
  void Wait();
  void Wake();
};
using Stop = StopRequests;

#else

inline void Ready(uint16_t) {}
inline void Failed(std::string_view) {}
using Stop = warpsql::StopSignals;

#endif

}  // namespace hardcharger::embedding
