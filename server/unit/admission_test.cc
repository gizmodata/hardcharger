// The statement queue's admission controller: ports GizmoSQL 1.x's
// tests/integration/test_admission_controller.cpp (all 14 cases) to Hardcharger's
// AdmissionController, which throws AdmissionController::Refused instead of returning
// an arrow::Status.
#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <mutex>
#include <thread>
#include <vector>

#include "admission.hpp"

using hardcharger::AdmissionController;
using hardcharger::AdmissionSlot;
using namespace std::chrono_literals;

namespace {

// Waits until `predicate` holds, up to 5 s.
template <typename Predicate>
bool Eventually(Predicate predicate) {
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (!predicate()) {
    if (std::chrono::steady_clock::now() > deadline) return false;
    std::this_thread::sleep_for(1ms);
  }
  return true;
}

AdmissionController::Refusal RefusalOf(AdmissionController& controller, int64_t wait,
                                       std::function<bool()> aborted = {},
                                       std::chrono::milliseconds poll = 0ms) {
  try {
    controller.Acquire(true, wait, std::move(aborted), poll);
  } catch (const AdmissionController::Refused& refused) {
    return refused.reason;
  }
  ADD_FAILURE() << "Acquire() took a slot";
  return AdmissionController::Refusal::kAborted;
}

}  // namespace

TEST(AdmissionControllerTest, DisabledLimitIsUnlimited) {
  AdmissionController controller;
  std::vector<AdmissionSlot> slots;
  for (int i = 0; i < 100; i++) slots.push_back(controller.Acquire(true, 0));
  for (const auto& slot : slots) EXPECT_FALSE(slot.holds_slot());
  EXPECT_EQ(controller.ActiveCount(), 0);
}

TEST(AdmissionControllerTest, EnforceFalseBypasses) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  EXPECT_TRUE(held.holds_slot());
  auto bypass = controller.Acquire(false, 0);
  EXPECT_FALSE(bypass.holds_slot());
  EXPECT_EQ(controller.ActiveCount(), 1);
}

TEST(AdmissionControllerTest, TracksAndCapsActive) {
  AdmissionController controller;
  controller.SetLimit(2);
  auto a = controller.Acquire(true, 0);
  auto b = controller.Acquire(true, 0);
  EXPECT_EQ(controller.ActiveCount(), 2);
  a.Release();
  EXPECT_EQ(controller.ActiveCount(), 1);
  {
    auto c = controller.Acquire(true, 0);
    EXPECT_EQ(controller.ActiveCount(), 2);
  }
  EXPECT_EQ(controller.ActiveCount(), 1);
}

TEST(AdmissionControllerTest, BlocksUntilSlotFrees) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<bool> admitted = false;
  std::thread waiter([&] {
    auto slot = controller.Acquire(true, 0);
    admitted = true;
  });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  std::this_thread::sleep_for(50ms);
  EXPECT_FALSE(admitted);
  held.Release();
  waiter.join();
  EXPECT_TRUE(admitted);
  EXPECT_EQ(controller.ActiveCount(), 0);
}

TEST(AdmissionControllerTest, ResizeUpWakesWaiters) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<int> admitted = 0;
  std::vector<std::thread> waiters;
  std::vector<AdmissionSlot> kept(2);
  std::mutex kept_mutex;
  for (int i = 0; i < 2; i++) {
    waiters.emplace_back([&, i] {
      auto slot = controller.Acquire(true, 0);
      std::lock_guard lock(kept_mutex);
      kept[i] = std::move(slot);
      admitted++;
    });
  }
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 2; }));
  controller.SetLimit(3);
  for (auto& w : waiters) w.join();
  EXPECT_EQ(admitted, 2);
  EXPECT_EQ(controller.ActiveCount(), 3);
}

TEST(AdmissionControllerTest, DisableWhileWaitingReleasesWaiters) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<bool> inert = false;
  std::thread waiter([&] {
    auto slot = controller.Acquire(true, 0);
    inert = !slot.holds_slot();
  });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  controller.SetLimit(0);
  waiter.join();
  EXPECT_TRUE(inert);
  EXPECT_EQ(controller.QueuedCount(), 0);
}

TEST(AdmissionControllerTest, MaxQueuedRejects) {
  AdmissionController controller;
  controller.SetLimit(1);
  controller.SetMaxQueued(1);
  auto held = controller.Acquire(true, 0);
  std::thread waiter([&] { auto slot = controller.Acquire(true, 0); });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  try {
    controller.Acquire(true, 0);
    ADD_FAILURE() << "a full queue admitted a statement";
  } catch (const AdmissionController::Refused& refused) {
    EXPECT_EQ(refused.reason, AdmissionController::Refusal::kQueueFull);
    EXPECT_NE(refused.message.find("queue is full"), std::string::npos) << refused.message;
  }
  held.Release();
  waiter.join();
}

TEST(AdmissionControllerTest, MaxQueueWaitTimesOut) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  const auto started = std::chrono::steady_clock::now();
  EXPECT_EQ(RefusalOf(controller, 1), AdmissionController::Refusal::kWaitedTooLong);
  EXPECT_GE(std::chrono::steady_clock::now() - started, 900ms);
  EXPECT_EQ(controller.QueuedCount(), 0);
}

TEST(AdmissionControllerTest, ConcurrencyNeverExceedsLimit) {
  AdmissionController controller;
  controller.SetLimit(4);
  std::atomic<int> active = 0;
  std::atomic<int> peak = 0;
  std::vector<std::thread> workers;
  for (int i = 0; i < 32; i++) {
    workers.emplace_back([&] {
      for (int j = 0; j < 20; j++) {
        auto slot = controller.Acquire(true, 0);
        const int now = ++active;
        int seen = peak.load();
        while (now > seen && !peak.compare_exchange_weak(seen, now)) {
        }
        std::this_thread::sleep_for(100us);
        --active;
      }
    });
  }
  for (auto& w : workers) w.join();
  EXPECT_LE(peak, 4);
  EXPECT_GE(peak, 1);
  EXPECT_EQ(controller.ActiveCount(), 0);
}

TEST(AdmissionControllerTest, AdmitsInFifoOrder) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::mutex order_mutex;
  std::vector<int> order;
  std::vector<std::thread> waiters;
  for (int i = 0; i < 5; i++) {
    waiters.emplace_back([&, i] {
      auto slot = controller.Acquire(true, 0);
      std::lock_guard lock(order_mutex);
      order.push_back(i);
    });
    // Start the next waiter only once this one is queued, so arrival order is known.
    ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == i + 1; }));
  }
  held.Release();
  for (auto& w : waiters) w.join();
  EXPECT_EQ(order, (std::vector<int>{0, 1, 2, 3, 4}));
}

TEST(AdmissionControllerTest, TimedOutWaiterIsRemovedFifoContinues) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<bool> timed_out = false;
  std::thread impatient([&] { timed_out = RefusalOf(controller, 1) == AdmissionController::Refusal::kWaitedTooLong; });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  std::atomic<bool> patient_admitted = false;
  std::thread patient([&] {
    auto slot = controller.Acquire(true, 0);
    patient_admitted = true;
  });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 2; }));
  impatient.join();
  EXPECT_TRUE(timed_out);
  EXPECT_EQ(controller.QueuedCount(), 1);
  held.Release();
  patient.join();
  EXPECT_TRUE(patient_admitted);
}

TEST(AdmissionControllerTest, AbortedWaiterIsCancelledViaWakeWaiters) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<bool> killed = false;
  std::atomic<bool> cancelled = false;
  std::thread waiter([&] {
    cancelled = RefusalOf(controller, 0, [&] { return killed.load(); }) == AdmissionController::Refusal::kAborted;
  });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  killed = true;
  controller.WakeWaiters();
  waiter.join();
  EXPECT_TRUE(cancelled);
  EXPECT_EQ(controller.QueuedCount(), 0);
  EXPECT_EQ(controller.ActiveCount(), 1);  // the aborted waiter took no slot
}

TEST(AdmissionControllerTest, AbortBeforeQueuingReturnsCancelled) {
  AdmissionController controller;
  controller.SetLimit(1);
  EXPECT_EQ(RefusalOf(controller, 0, [] { return true; }), AdmissionController::Refusal::kAborted);
  EXPECT_EQ(controller.ActiveCount(), 0);
  EXPECT_EQ(controller.QueuedCount(), 0);
}

TEST(AdmissionControllerTest, PolledAbortDoesNotNeedWakeOrSlotRelease) {
  AdmissionController controller;
  controller.SetLimit(1);
  auto held = controller.Acquire(true, 0);
  std::atomic<bool> cancelled_flag = false;
  std::atomic<bool> cancelled = false;
  std::thread waiter([&] {
    cancelled = RefusalOf(controller, 0, [&] { return cancelled_flag.load(); }, 20ms) ==
                AdmissionController::Refusal::kAborted;
  });
  ASSERT_TRUE(Eventually([&] { return controller.QueuedCount() == 1; }));
  cancelled_flag = true;  // no WakeWaiters(), no Release()
  waiter.join();
  EXPECT_TRUE(cancelled);
  EXPECT_TRUE(held.holds_slot());
}
