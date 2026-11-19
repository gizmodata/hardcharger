// Clang's thread-safety analysis (-Wthread-safety): which mutex guards which state, checked at
// compile time. The macros expand to nothing on other compilers (GCC, MSVC), so annotated code
// builds everywhere; clang builds (macOS, the ThreadSanitizer job) check it, and CI makes its
// findings errors.
//
// std::mutex is not annotated by libstdc++ (nor by libc++ unless asked), so state that a mutex
// guards uses hardcharger::Mutex, a std::mutex the analysis knows as a capability, locked by
// MutexLock (a lock_guard) or UniqueMutexLock (a unique_lock, for condition variables and early
// unlocks). A condition variable waits on UniqueMutexLock::native(); a wait predicate that reads
// guarded state says so: [this]() HC_REQUIRES(mutex_) { return stopping_; }.
//
// https://clang.llvm.org/docs/ThreadSafetyAnalysis.html
#pragma once

#include <mutex>

#if defined(__clang__)
#define HC_THREAD_ANNOTATION(x) __attribute__((x))
#else
#define HC_THREAD_ANNOTATION(x)
#endif

// A type that is a lock (a capability).
#define HC_CAPABILITY(x) HC_THREAD_ANNOTATION(capability(x))
// An RAII type that holds a capability for its lifetime.
#define HC_SCOPED_CAPABILITY HC_THREAD_ANNOTATION(scoped_lockable)
// Data that may be read or written only while holding the capability.
#define HC_GUARDED_BY(x) HC_THREAD_ANNOTATION(guarded_by(x))
// The data a pointer points to is guarded (the pointer itself is not).
#define HC_PT_GUARDED_BY(x) HC_THREAD_ANNOTATION(pt_guarded_by(x))
// The caller must hold the capability (exclusively).
#define HC_REQUIRES(...) HC_THREAD_ANNOTATION(requires_capability(__VA_ARGS__))
// The caller must not hold the capability (the function takes it itself).
#define HC_EXCLUDES(...) HC_THREAD_ANNOTATION(locks_excluded(__VA_ARGS__))
// The function acquires / releases the capability.
#define HC_ACQUIRE(...) HC_THREAD_ANNOTATION(acquire_capability(__VA_ARGS__))
#define HC_RELEASE(...) HC_THREAD_ANNOTATION(release_capability(__VA_ARGS__))
#define HC_TRY_ACQUIRE(...) HC_THREAD_ANNOTATION(try_acquire_capability(__VA_ARGS__))
// The function returns a reference to the capability.
#define HC_RETURN_CAPABILITY(x) HC_THREAD_ANNOTATION(lock_returned(x))
// Turns the analysis off for one function; say why next to each use.
#define HC_NO_THREAD_SAFETY_ANALYSIS HC_THREAD_ANNOTATION(no_thread_safety_analysis)

namespace hardcharger {

// A std::mutex the analysis knows. Lockable, so std::scoped_lock and friends still work on it.
class HC_CAPABILITY("mutex") Mutex {
 public:
  Mutex() = default;
  Mutex(const Mutex&) = delete;
  Mutex& operator=(const Mutex&) = delete;

  void lock() HC_ACQUIRE() { mutex_.lock(); }
  void unlock() HC_RELEASE() { mutex_.unlock(); }
  bool try_lock() HC_TRY_ACQUIRE(true) { return mutex_.try_lock(); }

  // The std::mutex itself, for UniqueMutexLock (and so a std::condition_variable).
  std::mutex& native() { return mutex_; }

 private:
  std::mutex mutex_;
};

// std::lock_guard for Mutex.
class HC_SCOPED_CAPABILITY MutexLock {
 public:
  explicit MutexLock(Mutex& mutex) HC_ACQUIRE(mutex) : mutex_(mutex) { mutex_.lock(); }
  ~MutexLock() HC_RELEASE() { mutex_.unlock(); }
  MutexLock(const MutexLock&) = delete;
  MutexLock& operator=(const MutexLock&) = delete;

 private:
  Mutex& mutex_;
};

// std::unique_lock for Mutex: native() for a std::condition_variable's wait, Unlock() and Lock()
// to let go of the mutex for a while (the destructor unlocks only if it is held).
class HC_SCOPED_CAPABILITY UniqueMutexLock {
 public:
  explicit UniqueMutexLock(Mutex& mutex) HC_ACQUIRE(mutex) : lock_(mutex.native()) {}
  ~UniqueMutexLock() HC_RELEASE() {}
  UniqueMutexLock(const UniqueMutexLock&) = delete;
  UniqueMutexLock& operator=(const UniqueMutexLock&) = delete;

  void Lock() HC_ACQUIRE() { lock_.lock(); }
  void Unlock() HC_RELEASE() { lock_.unlock(); }

  std::unique_lock<std::mutex>& native() { return lock_; }

 private:
  std::unique_lock<std::mutex> lock_;
};

}  // namespace hardcharger
