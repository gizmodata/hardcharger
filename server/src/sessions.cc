#include "sessions.hpp"

#include <algorithm>

#include "engine.hpp"

namespace hardcharger {

using warpsql::Error;
using warpsql::ErrorCode;

bool SessionRegistry::Admit(const warpsql::Principal& principal) {
  if (principal.role == "admin" || max_sessions_ <= 0) return false;
  std::lock_guard lock(mutex_);
  if (capped_in_use_ >= max_sessions_) {
    throw Error(ErrorCode::kTooManySessions,
                "this server already has its max-sessions limit of " + std::to_string(max_sessions_) +
                    " non-admin sessions open (Unavailable); close one, or retry later");
  }
  capped_in_use_++;
  return true;
}

void SessionRegistry::Unadmit(bool capped) {
  if (!capped) return;
  std::lock_guard lock(mutex_);
  capped_in_use_ = std::max<int64_t>(0, capped_in_use_ - 1);
}

void SessionRegistry::Add(Session* session, bool capped) {
  std::lock_guard lock(mutex_);
  sessions_[session->id()] = {session, capped};
}

void SessionRegistry::Remove(Session* session) {
  std::lock_guard lock(mutex_);
  auto it = sessions_.find(session->id());
  if (it == sessions_.end()) return;
  if (it->second.capped) capped_in_use_ = std::max<int64_t>(0, capped_in_use_ - 1);
  sessions_.erase(it);
}

SessionRegistry::KillResult SessionRegistry::Kill(const std::string& id, const Session& by) {
  std::string wanted = id;
  std::transform(wanted.begin(), wanted.end(), wanted.begin(), [](unsigned char c) { return std::tolower(c); });
  std::lock_guard lock(mutex_);
  auto it = sessions_.find(wanted);
  if (it == sessions_.end() || it->second.session->killed()) return KillResult::kNotFound;
  if (it->second.session == &by) return KillResult::kOwnSession;
  // The session stays listed until the SDK lets it go, but its place is free at once.
  if (it->second.capped) {
    capped_in_use_ = std::max<int64_t>(0, capped_in_use_ - 1);
    it->second.capped = false;
  }
  it->second.session->Kill();
  return KillResult::kKilled;
}

std::vector<SessionSummary> SessionRegistry::List() const {
  std::lock_guard lock(mutex_);
  std::vector<SessionSummary> out;
  for (const auto& [id, entry] : sessions_) {
    const Session& s = *entry.session;
    if (s.killed()) continue;
    out.push_back({id, s.principal().name, s.principal().role, s.principal().auth_method, s.started_at_us(),
                   s.running()});
  }
  return out;
}

std::size_t SessionRegistry::size() const {
  std::lock_guard lock(mutex_);
  return static_cast<std::size_t>(
      std::count_if(sessions_.begin(), sessions_.end(), [](const auto& item) { return !item.second.session->killed(); }));
}

std::size_t SessionRegistry::capped_in_use() const {
  std::lock_guard lock(mutex_);
  return static_cast<std::size_t>(capped_in_use_);
}

void SessionRegistry::ForEach(const std::function<void(Session&)>& fn) const {
  std::lock_guard lock(mutex_);
  for (const auto& [id, entry] : sessions_) fn(*entry.session);
}

}  // namespace hardcharger
