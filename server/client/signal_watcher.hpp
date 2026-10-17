// Runs a callback on Ctrl-C. POSIX: SIGINT is blocked in every thread and a dedicated
// thread takes it with sigwait(), so the callback runs as ordinary code (it makes HTTP
// requests to cancel the statement), not in a signal handler. Windows: a console control
// handler, which Windows already runs on a thread of its own.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdlib>
#include <functional>
#include <mutex>
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

// Windows: a console control handler. It only counts the Ctrl-C and hands it to a thread
// of the watcher's own, which runs the callback as ordinary code, as on POSIX (Windows
// would otherwise run it on the handler's thread, and the handler must return quickly).
// Ctrl+Break, which a parent sends to a child it started in its own process group, is
// SIGTERM's counterpart: the client exits at once, as it does on SIGTERM. Closing the
// console and shutdown take Windows' default (the process ends).
class SignalWatcher {
 public:
  explicit SignalWatcher(std::function<void()> on_interrupt) : callback_(std::move(on_interrupt)) {
    Current() = this;
    thread_ = std::thread([this] {
      std::unique_lock lock(mutex_);
      for (;;) {
        wake_.wait(lock, [this] { return stopped_ || pending_ > 0; });
        if (stopped_) return;
        pending_--;
        lock.unlock();
        callback_();
        lock.lock();
      }
    });
    SetConsoleCtrlHandler(&Handler, TRUE);
  }

  ~SignalWatcher() {
    SetConsoleCtrlHandler(&Handler, FALSE);
    {
      std::lock_guard lock(mutex_);
      stopped_ = true;
    }
    wake_.notify_all();
    thread_.join();
    Current() = nullptr;
  }

  SignalWatcher(const SignalWatcher&) = delete;
  SignalWatcher& operator=(const SignalWatcher&) = delete;

 private:
  static std::atomic<SignalWatcher*>& Current() {
    static std::atomic<SignalWatcher*> current{nullptr};
    return current;
  }

  static BOOL WINAPI Handler(DWORD event) {
    if (event == CTRL_BREAK_EVENT) std::_Exit(0);
    if (event != CTRL_C_EVENT) return FALSE;
    if (SignalWatcher* watcher = Current().load()) {
      {
        std::lock_guard lock(watcher->mutex_);
        watcher->pending_++;
      }
      watcher->wake_.notify_one();
    }
    return TRUE;  // handled: the process goes on
  }

  std::function<void()> callback_;
  std::mutex mutex_;
  std::condition_variable wake_;
  int pending_ = 0;
  bool stopped_ = false;
  std::thread thread_;
};

#endif

}  // namespace hardcharger::client
