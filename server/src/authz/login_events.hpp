// Sign-in records the access-control store keeps for the login guard (login_guard.hpp):
// failed and successful password sign-ins, and an administrator clearing a lock. Every
// replica, and hardcharger-server --verify-credentials, adds its own and reads the others',
// so a lock holds wherever the next attempt arrives.
#pragma once

#include <cstdint>
#include <string>

namespace hardcharger::authz {

struct LoginEventRecord {
  std::string id;          // random, unique
  int64_t at_ms = 0;       // milliseconds since the Unix epoch
  std::string kind;        // "failure", "success" or "clear"
  std::string user_key;    // the folded user name (empty in a clear: every user)
  std::string user_name;   // as the client sent it, cut for display
  std::string address;     // the client's address key (empty: none, or in a clear every address)
  std::string origin;      // the instance that recorded it
};

}  // namespace hardcharger::authz
