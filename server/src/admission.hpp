// The statement queue: caps how many statements
// run at once; the rest wait their turn, first come first served.
//
// Waiters are admitted in strict arrival order, and a new statement never overtakes one
// already waiting. The limits can change while statements wait (SET GLOBAL). A waiter
// leaves the queue at once when its abort condition turns true (its session was killed
// or its statement cancelled) and WakeWaiters() is called, without taking a slot.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <list>
#include <mutex>

namespace hardcharger {

class AdmissionController;

// One held slot; gives it back when destroyed. An empty slot holds nothing.
class AdmissionSlot {
 public:
  AdmissionSlot() = default;
  AdmissionSlot(const AdmissionSlot&) = delete;
  AdmissionSlot& operator=(const AdmissionSlot&) = delete;
  AdmissionSlot(AdmissionSlot&& other) noexcept;
  AdmissionSlot& operator=(AdmissionSlot&& other) noexcept;
  ~AdmissionSlot() { Release(); }

  bool holds_slot() const { return controller_ != nullptr; }
  void Release();

 private:
  friend class AdmissionController;
  explicit AdmissionSlot(AdmissionController* controller) : controller_(controller) {}
  AdmissionController* controller_ = nullptr;
};

class AdmissionController {
 public:
  // How an Acquire() that got no slot ended. Thrown as warpsql::Error by the server.
  enum class Refusal { kQueueFull, kWaitedTooLong, kAborted };
  struct Refused {
    Refusal reason;
    std::string message;
  };

  AdmissionController() = default;
  AdmissionController(const AdmissionController&) = delete;
  AdmissionController& operator=(const AdmissionController&) = delete;

  // Statements running at once; 0 or less: unlimited (every waiter proceeds).
  void SetLimit(int64_t limit);
  // Statements that may wait at once; 0: unbounded; negative: 8 x the limit (kept as
  // given, so it follows a later SetLimit), as the max_queued_statements option has it.
  void SetMaxQueued(int64_t max_queued);
  // The wait used when a statement has no limit of its own; 0 or less: forever.
  void SetDefaultMaxQueueWaitSeconds(int64_t seconds);

  int64_t Limit() const;
  int64_t MaxQueued() const;           // as set (-1 for 8 x the limit)
  int64_t EffectiveMaxQueued() const;  // the bound in force (0: unbounded)
  int64_t DefaultMaxQueueWaitSeconds() const;
  int64_t ActiveCount() const;
  int64_t QueuedCount() const;

  // A slot for one statement. Without `enforce`, or with no limit, an empty slot at
  // once. Otherwise a free slot when nobody waits ahead; else waits (up to
  // `max_wait_seconds`, 0 or less: forever) unless the queue is full. `is_aborted` is
  // checked on entry and whenever the waiter wakes (WakeWaiters(), or every
  // `abort_poll` when positive); it runs under the controller's lock, so it must be
  // cheap and must not call back in. Throws Refused.
  AdmissionSlot Acquire(bool enforce, int64_t max_wait_seconds, std::function<bool()> is_aborted = {},
                        std::chrono::milliseconds abort_poll = std::chrono::milliseconds::zero());

  // Makes every waiter re-check its abort condition.
  void WakeWaiters();

 private:
  friend class AdmissionSlot;
  enum class WaiterState { kWaiting, kGranted, kGrantedUnlimited };
  struct Waiter {
    std::condition_variable cv;
    WaiterState state = WaiterState::kWaiting;
  };

  void Promote();  // mutex_ held
  int64_t QueueBound() const;  // mutex_ held
  void ReleaseSlot();

  mutable std::mutex mutex_;
  int64_t limit_ = 0;
  int64_t max_queued_ = 0;
  int64_t default_wait_seconds_ = 0;
  int64_t active_ = 0;
  std::list<Waiter*> waiters_;  // oldest first
};

}  // namespace hardcharger
