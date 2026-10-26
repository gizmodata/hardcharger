#include "admission.hpp"

#include <string>
#include <utility>

namespace hardcharger {

AdmissionSlot::AdmissionSlot(AdmissionSlot&& other) noexcept
    : controller_(std::exchange(other.controller_, nullptr)) {}

AdmissionSlot& AdmissionSlot::operator=(AdmissionSlot&& other) noexcept {
  if (this != &other) {
    Release();
    controller_ = std::exchange(other.controller_, nullptr);
  }
  return *this;
}

void AdmissionSlot::Release() {
  if (controller_ != nullptr) {
    std::exchange(controller_, nullptr)->ReleaseSlot();
  }
}

void AdmissionController::SetLimit(int64_t limit) {
  std::lock_guard lock(mutex_);
  limit_ = limit;
  if (limit_ <= 0) {
    // Unlimited now: everyone waiting goes ahead, untracked.
    for (Waiter* waiter : waiters_) {
      waiter->state = WaiterState::kGrantedUnlimited;
      waiter->cv.notify_one();
    }
    waiters_.clear();
  } else {
    Promote();  // a higher limit frees slots for the oldest waiters
  }
}

void AdmissionController::SetMaxQueued(int64_t max_queued) {
  std::lock_guard lock(mutex_);
  max_queued_ = max_queued;
}

void AdmissionController::SetDefaultMaxQueueWaitSeconds(int64_t seconds) {
  std::lock_guard lock(mutex_);
  default_wait_seconds_ = seconds < 0 ? 0 : seconds;
}

AdmissionController::Snapshot AdmissionController::GetSnapshot() const {
  std::lock_guard lock(mutex_);
  return {limit_, active_, static_cast<int64_t>(waiters_.size()), QueueBound(), default_wait_seconds_};
}

int64_t AdmissionController::Limit() const {
  std::lock_guard lock(mutex_);
  return limit_;
}

int64_t AdmissionController::MaxQueued() const {
  std::lock_guard lock(mutex_);
  return max_queued_;
}

int64_t AdmissionController::QueueBound() const { return max_queued_ < 0 ? 8 * limit_ : max_queued_; }

int64_t AdmissionController::EffectiveMaxQueued() const {
  std::lock_guard lock(mutex_);
  return QueueBound();
}

int64_t AdmissionController::DefaultMaxQueueWaitSeconds() const {
  std::lock_guard lock(mutex_);
  return default_wait_seconds_;
}

int64_t AdmissionController::ActiveCount() const {
  std::lock_guard lock(mutex_);
  return active_;
}

int64_t AdmissionController::QueuedCount() const {
  std::lock_guard lock(mutex_);
  return static_cast<int64_t>(waiters_.size());
}

AdmissionSlot AdmissionController::Acquire(bool enforce, int64_t max_wait_seconds,
                                           std::function<bool()> is_aborted,
                                           std::chrono::milliseconds abort_poll) {
  if (!enforce) return {};
  std::unique_lock lock(mutex_);
  if (limit_ <= 0) return {};
  if (is_aborted && is_aborted()) {
    throw Refused{Refusal::kAborted, "the statement was cancelled while queued"};
  }
  // Free capacity is only ever there when nobody waits (releases hand it to waiters
  // first), so taking it here never overtakes a waiter.
  if (active_ < limit_ && waiters_.empty()) {
    ++active_;
    return AdmissionSlot(this);
  }
  if (const int64_t bound = QueueBound(); bound > 0 && static_cast<int64_t>(waiters_.size()) >= bound) {
    throw Refused{Refusal::kQueueFull, "the statement queue is full (max_concurrent_statements=" +
                                           std::to_string(limit_) + ", max_queued_statements=" +
                                           std::to_string(bound) + "); retry shortly"};
  }

  Waiter self;
  waiters_.push_back(&self);
  const auto position = std::prev(waiters_.end());
  const auto done = [&] { return self.state != WaiterState::kWaiting || (is_aborted && is_aborted()); };
  const auto deadline = max_wait_seconds > 0
                            ? std::chrono::steady_clock::now() + std::chrono::seconds(max_wait_seconds)
                            : std::chrono::steady_clock::time_point::max();
  bool finished = done();
  while (!finished && std::chrono::steady_clock::now() < deadline) {
    auto until = deadline;
    if (is_aborted && abort_poll > std::chrono::milliseconds::zero()) {
      until = std::min(deadline, std::chrono::steady_clock::now() + abort_poll);
    }
    if (until == std::chrono::steady_clock::time_point::max()) {
      self.cv.wait(lock, done);
      finished = true;
    } else {
      finished = self.cv.wait_until(lock, until, done);
    }
  }
  // Nobody granted us anything: we are still in the queue and must leave it ourselves.
  // An abort is reported as such even when it races the deadline.
  if (self.state == WaiterState::kWaiting) {
    waiters_.erase(position);
    if (is_aborted && is_aborted()) {
      throw Refused{Refusal::kAborted, "the statement was cancelled while queued"};
    }
    throw Refused{Refusal::kWaitedTooLong, "the statement waited longer than max_queue_wait (" +
                                               std::to_string(max_wait_seconds) + "s) for a slot; retry shortly"};
  }
  return self.state == WaiterState::kGranted ? AdmissionSlot(this) : AdmissionSlot();
}

void AdmissionController::WakeWaiters() {
  std::lock_guard lock(mutex_);
  for (Waiter* waiter : waiters_) waiter->cv.notify_one();
}

void AdmissionController::Promote() {
  while (limit_ > 0 && active_ < limit_ && !waiters_.empty()) {
    Waiter* waiter = waiters_.front();
    waiters_.pop_front();
    waiter->state = WaiterState::kGranted;
    ++active_;
    waiter->cv.notify_one();
  }
}

void AdmissionController::ReleaseSlot() {
  std::lock_guard lock(mutex_);
  if (active_ > 0) --active_;
  Promote();
}

}  // namespace hardcharger
