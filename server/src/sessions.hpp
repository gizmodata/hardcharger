// The server's view of its client sessions, beside the Chargewire SDK's own session table:
// session ids (UUIDs, as HARDCHARGER_CURRENT_SESSION() returns them), the
// max-sessions cap and KILL SESSION.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include <optional>

#include "chargewire/backend.hpp"
#include "chargewire/server.hpp"

namespace hardcharger {

class Session;
struct Runtime;

// Whether `viewer` may see (and cancel) `target`: an administrator without catalog_access
// rules sees every session; anyone else, a restricted administrator included, the sessions
// of its own principal (the same user name and tenant), never a more privileged one's.
// hardcharger_sessions(), hardcharger_active_connections() and hardcharger_cancel() all
// decide by this.
bool SeesSession(const Session& viewer, const Session& target);

// Stops what session `id` runs, for `reason`, through Chargewire's cancel: its statements
// end cancelled with that cancel_reason (a queued one leaves the statement queue), result
// readers get an incomplete stream, open partitioned ingests and ingest bodies still
// arriving are aborted. The session lives on. The one implementation hardcharger_cancel()
// and KILL SESSION share (KILL then ends the session). Returns how many statements and
// ingests it stopped; nullopt when the server has no such session.
std::optional<std::size_t> CancelSessionWork(Runtime& runtime, const std::string& id,
                                             chargewire::SessionCancelReason reason);

struct SessionSummary {
  std::string session_id;
  std::string username;
  std::string role;
  std::string auth_method;
  int64_t started_at_us = 0;  // since the epoch
  bool running = false;       // a statement is running
};

class SessionRegistry {
 public:
  explicit SessionRegistry(int64_t max_sessions) : max_sessions_(max_sessions) {}

  // Takes a place for a new session of `principal`. Admins are never capped; anyone
  // else gets too_many_sessions once `max_sessions` non-admin sessions are open.
  // Returns whether the session holds a capped place (pass it to Add()).
  bool Admit(const chargewire::Principal& principal);
  // Gives back a place Admit() took, for a session that could not be opened.
  void Unadmit(bool capped);

  void Add(Session* session, bool capped);
  // Removes `session` and frees its place (unless a kill already did).
  void Remove(Session* session);

  // Session `id` (any case) as `viewer` finds it.
  enum class Lookup { kNotFound, kOwnSession, kHidden, kVisible };
  Lookup Find(const std::string& id, const Session& viewer) const;

  enum class KillResult { kKilled, kNotFound, kOwnSession };
  // Marks session `id` killed for `by`: frees its place, and makes its next request fail
  // with session_ended. What it runs is stopped by CancelSessionWork() first.
  KillResult Kill(const std::string& id, const Session& by);

  // The live sessions `viewer` sees (SeesSession).
  std::vector<SessionSummary> List(const Session& viewer) const;
  std::size_t size() const;
  std::size_t capped_in_use() const;
  int64_t max_sessions() const { return max_sessions_; }

  // Runs `fn` on every live session, under the registry's lock (so none is destroyed
  // meanwhile). `fn` must not call back into the registry.
  void ForEach(const std::function<void(Session&)>& fn) const;

 private:
  struct Entry {
    Session* session;
    bool capped;
  };

  const int64_t max_sessions_;
  mutable std::mutex mutex_;
  std::map<std::string, Entry> sessions_;  // by session id
  int64_t capped_in_use_ = 0;
};

}  // namespace hardcharger
