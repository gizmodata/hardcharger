// The server's view of its client sessions, beside the Warp SDK's own session table:
// GizmoSQL-style session ids (UUIDs, as GIZMOSQL_CURRENT_SESSION() returns them), the
// max-sessions cap and KILL SESSION.
#pragma once

#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include "warpsql/backend.hpp"

namespace hardcharger {

class Session;

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
  bool Admit(const warpsql::Principal& principal);
  // Gives back a place Admit() took, for a session that could not be opened.
  void Unadmit(bool capped);

  void Add(Session* session, bool capped);
  // Removes `session` and frees its place (unless a kill already did).
  void Remove(Session* session);

  enum class KillResult { kKilled, kNotFound, kOwnSession };
  // Ends session `id` for `by`: interrupts what it runs, frees its place, and makes its
  // next request fail with session_ended.
  KillResult Kill(const std::string& id, const Session& by);

  std::vector<SessionSummary> List() const;
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
