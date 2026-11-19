// The embedded server (embedded/hardcharger_embedded.h): hardcharger-server's main(),
// built as hardcharger_server_main (server/CMakeLists.txt), run on a thread of its own,
// with the hooks of embedding.hpp reporting back to the app. Only in the
// hardcharger_embedded library.
#include "embedding.hpp"

#include <algorithm>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <exception>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "duckdb_v2.h"
#include "hardcharger_embedded.h"
#include "third_party_notices.hpp"
#include "thread_annotations.hpp"
#include "version.hpp"
#include "web_ui.hpp"

// main.cc, compiled with main renamed (server/CMakeLists.txt).
int hardcharger_server_main(int argc, char** argv);

#ifdef HARDCHARGER_STATIC_DUCKDB
// DuckDB's generated loader (server/CMakeLists.txt, HARDCHARGER_STATIC): registers the
// built-in extensions. A program has it called before main() by compiling DuckDB's
// static_extension_autoregister.cpp into itself; an app links this library from an
// archive, where nothing would pull that file in, so the first start calls the loader.
// Registering again is a no-op.
extern "C" int32_t duckdb_register_static_extensions(void);
#endif

using hardcharger::Mutex;
using hardcharger::MutexLock;
using hardcharger::UniqueMutexLock;

struct hardcharger_server {
  Mutex mutex;
  std::condition_variable changed;
  // From the server's thread.
  bool ready HC_GUARDED_BY(mutex) = false;
  bool exited HC_GUARDED_BY(mutex) = false;
  int exit_status HC_GUARDED_BY(mutex) = 0;
  int port HC_GUARDED_BY(mutex) = 0;
  std::string failure HC_GUARDED_BY(mutex);
  // From the app: stop requests StopRequests::Wait() hasn't taken yet.
  int stop_requests HC_GUARDED_BY(mutex) = 0;
  std::thread thread;
};

namespace {

// The running server: one per process, which the hooks report to. Set before its thread
// starts and cleared once that thread has been joined.
Mutex current_mutex;
hardcharger_server* current HC_GUARDED_BY(current_mutex) = nullptr;

hardcharger_server* Current() {
  MutexLock lock(current_mutex);
  return current;
}

void CopyError(const std::string& message, char* error, size_t error_size) {
  if (error == nullptr || error_size == 0) return;
  const size_t n = std::min(message.size(), error_size - 1);
  std::memcpy(error, message.data(), n);
  error[n] = '\0';
}

}  // namespace

namespace hardcharger::embedding {

void Ready(uint16_t port) {
  hardcharger_server* server = Current();
  if (server == nullptr) return;
  {
    MutexLock lock(server->mutex);
    server->ready = true;
    server->port = port;
  }
  server->changed.notify_all();
}

void Failed(std::string_view message) {
  hardcharger_server* server = Current();
  if (server == nullptr) return;
  MutexLock lock(server->mutex);
  if (server->failure.empty()) server->failure = std::string(message);
}

void StopRequests::Wait() {
  hardcharger_server* server = Current();
  if (server == nullptr) return;
  UniqueMutexLock lock(server->mutex);
  server->changed.wait(lock.native(), [&]() HC_REQUIRES(server->mutex) { return server->stop_requests > 0; });
  server->stop_requests--;
}

void StopRequests::Wake() {
  hardcharger_server* server = Current();
  if (server == nullptr) return;
  {
    MutexLock lock(server->mutex);
    server->stop_requests++;
  }
  server->changed.notify_all();
}

}  // namespace hardcharger::embedding

extern "C" {

int hardcharger_server_start(int argc, const char* const* argv, hardcharger_server** out, char* error,
                             size_t error_size) {
  if (out != nullptr) *out = nullptr;
  if (out == nullptr || argc < 1 || argv == nullptr) {
    CopyError("hardcharger_server_start: needs argv (argv[0] at least) and somewhere to put the server", error,
              error_size);
    return 2;
  }
#ifdef HARDCHARGER_STATIC_DUCKDB
  static std::once_flag registered;
  std::call_once(registered, [] { duckdb_register_static_extensions(); });
#endif
  auto* server = new hardcharger_server();
  {
    MutexLock lock(current_mutex);
    if (current != nullptr) {
      delete server;
      CopyError("a Hardcharger server is already running in this process", error, error_size);
      return 3;
    }
    current = server;
  }
  // main() takes char**, and may keep pointers into it while it runs: the thread owns the
  // copies until it ends.
  std::vector<std::string> args(argv, argv + argc);
  server->thread = std::thread([server, args = std::move(args)]() mutable {
    std::vector<char*> pointers;
    for (auto& arg : args) pointers.push_back(arg.data());
    pointers.push_back(nullptr);
    int status = 1;
    try {
      status = hardcharger_server_main(static_cast<int>(args.size()), pointers.data());
    } catch (const std::exception& e) {
      hardcharger::embedding::Failed(e.what());
    } catch (...) {
      hardcharger::embedding::Failed("the server stopped on an unknown exception");
    }
    {
      MutexLock lock(server->mutex);
      server->exited = true;
      server->exit_status = status;
    }
    server->changed.notify_all();
  });
  UniqueMutexLock lock(server->mutex);
  server->changed.wait(lock.native(), [&]() HC_REQUIRES(server->mutex) { return server->ready || server->exited; });
  if (server->ready) {
    *out = server;
    return 0;
  }
  // It ended before it served: hand back why, and free it.
  const int status = server->exit_status == 0 ? 1 : server->exit_status;
  std::string why = server->failure.empty() ? "the server stopped before it served (see its log)" : server->failure;
  lock.Unlock();
  server->thread.join();
  {
    MutexLock current_lock(current_mutex);
    current = nullptr;
  }
  delete server;
  CopyError(why, error, error_size);
  return status;
}

int hardcharger_server_port(const hardcharger_server* server) {
  if (server == nullptr) return 0;
  auto* s = const_cast<hardcharger_server*>(server);
  MutexLock lock(s->mutex);
  return s->port;
}

void hardcharger_server_request_stop(hardcharger_server* server, int immediately) {
  if (server == nullptr) return;
  {
    MutexLock lock(server->mutex);
    if (server->exited) return;
    server->stop_requests += immediately ? 2 : 1;
  }
  server->changed.notify_all();
}

int hardcharger_server_is_running(const hardcharger_server* server) {
  if (server == nullptr) return 0;
  auto* s = const_cast<hardcharger_server*>(server);
  MutexLock lock(s->mutex);
  return s->exited ? 0 : 1;
}

int hardcharger_server_wait(hardcharger_server* server, char* error, size_t error_size) {
  if (server == nullptr) return 0;
  UniqueMutexLock lock(server->mutex);
  server->changed.wait(lock.native(), [&]() HC_REQUIRES(server->mutex) { return server->exited; });
  const int status = server->exit_status;
  if (status != 0) {
    CopyError(server->failure.empty() ? "the server stopped on an error (see its log)" : server->failure, error,
              error_size);
  }
  return status;
}

void hardcharger_server_free(hardcharger_server* server) {
  if (server == nullptr) return;
  // The thread sets `exited` as its last act; joining it is what makes the server free to go.
  if (server->thread.joinable()) server->thread.join();
  {
    MutexLock lock(current_mutex);
    if (current == server) current = nullptr;
  }
  delete server;
}

const char* hardcharger_embedded_version(void) { return hardcharger::kVersion; }

const char* hardcharger_embedded_duckdb_version(void) {
  static const std::string version = [] {
    duckdb_v2_str v{};
    duckdb_v2_library_version(&v, nullptr);
    return std::string(v.ptr, v.len);
  }();
  return version.c_str();
}

const char* hardcharger_embedded_chargewire_version(void) { return HARDCHARGER_CHARGEWIRE_VERSION; }

const char* hardcharger_embedded_web_ui_version(void) {
  static const std::string version(hardcharger::EmbeddedWebUiVersion());
  return version.c_str();
}

const char* hardcharger_embedded_third_party_notices(void) {
  // As hardcharger-server --licenses: THIRD_PARTY_NOTICES.md, then the web UI's own.
  static const std::string notices = [] {
    std::string text(hardcharger::ThirdPartyNotices());
    if (const auto ui = hardcharger::EmbeddedWebUiFileContent(hardcharger::EmbeddedWebUiFiles(), "third-party-notices.txt")) {
      text += "\n\n";
      text += *ui;
    }
    return text;
  }();
  return notices.c_str();
}

}  // extern "C"
