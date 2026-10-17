// Runs a callback on Ctrl-C. POSIX: SIGINT is blocked in every thread and a dedicated
// thread takes it with sigwait(), so the callback runs as ordinary code (it makes HTTP
// requests to cancel the statement), not in a signal handler. Windows: a console control
// handler, which Windows already runs on a thread of its own.
#pragma once

#include <atomic>
#include <functional>
#include <system_error>
#include <thread>

#ifndef _WIN32
#include <csignal>
#include <pthread.h>
#else
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace hardcharger::client {

#ifndef _WIN32

class SignalWatcher {
 public:
  explicit SignalWatcher(std::function<void()> on_interrupt) {
    sigemptyset(&mask_);
    sigaddset(&mask_, SIGINT);
    const int error = pthread_sigmask(SIG_BLOCK, &mask_, &previous_mask_);
    if (error) throw std::system_error(error, std::generic_category());
    try {
      thread_ = std::thread([this, callback = std::move(on_interrupt)] {
        while (!stopped_.load()) {
          int signal = 0;
          if (sigwait(&mask_, &signal) != 0) return;
          if (stopped_.load()) return;
          if (signal == SIGINT) callback();
        }
      });
    } catch (...) {
      pthread_sigmask(SIG_SETMASK, &previous_mask_, nullptr);
      throw;
    }
  }

  ~SignalWatcher() {
    stopped_.store(true);
    // A thread-directed signal wakes only our waiter, including when shutdown
    // races with an in-flight callback. No detached thread survives this scope.
    pthread_kill(thread_.native_handle(), SIGINT);
    thread_.join();
    pthread_sigmask(SIG_SETMASK, &previous_mask_, nullptr);
  }

  SignalWatcher(const SignalWatcher&) = delete;
  SignalWatcher& operator=(const SignalWatcher&) = delete;

 private:
  sigset_t mask_{};
  sigset_t previous_mask_{};
  std::atomic<bool> stopped_{false};
  std::thread thread_;
};

#else

// TODO(windows): not built or tested on Windows yet; the Windows port finishes it.
class SignalWatcher {
 public:
  explicit SignalWatcher(std::function<void()> on_interrupt) {
    callback() = std::move(on_interrupt);
    SetConsoleCtrlHandler(&Handler, TRUE);
  }
  ~SignalWatcher() {
    SetConsoleCtrlHandler(&Handler, FALSE);
    callback() = nullptr;
  }
  SignalWatcher(const SignalWatcher&) = delete;
  SignalWatcher& operator=(const SignalWatcher&) = delete;

 private:
  static std::function<void()>& callback() {
    static std::function<void()> instance;
    return instance;
  }
  static BOOL WINAPI Handler(DWORD event) {
    if (event != CTRL_C_EVENT && event != CTRL_BREAK_EVENT) return FALSE;
    if (auto& cb = callback()) cb();
    return TRUE;  // handled: the process goes on
  }
};

#endif

}  // namespace hardcharger::client
