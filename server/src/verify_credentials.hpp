// hardcharger-server --verify-credentials: a one-shot decision on a credential, made with
// the server's own sign-in code and configuration (the administrator, the access-control
// store, the token keys, the secret key) and nothing written anywhere. For a control plane
// that must know whether a credential would sign in before it spends money starting the
// server (GizmoData Cloud's auto-resume): it runs this with the stopped server's own
// configuration and secrets, instead of holding a copy of its store credentials.
//
// A password check goes through the same login guard as the server's (login_guard.hpp):
// a (user, address) pair the server locked is refused here too, a wrong password counts
// toward the limits, a right one resets its pair. That is the one thing written: sign-in
// records in the access-control store (nothing else in it changes).
//
// stdin: one JSON object, exactly what a client sends to /chargewire/v1/auth/token, plus,
// optionally, the client's address as the server would see it ("client_address"; without
// it only the per-user limits apply):
//   {"grant_type": "password", "username": "...", "password": "...", "client_address": "203.0.113.9"}
//   {"grant_type": "password", "username": "token", "password": "<scoped token>"}
//   {"grant_type": "token_exchange", "subject_token": "<sign-in token>"}
// stdout: one JSON line:
//   {"verified": true, "user": "...", "role": "admin|user|readonly", "method": "...",
//    "would_provision": false}
//   {"verified": false, "reason": "...", "message": "..."}
//   {"verified": false, "reason": "rate_limited", "retry_after_seconds": 840, "message": "..."}
// Logs go to stderr. The credential is never printed or logged.
#pragma once

#include <iosfwd>

#include "config.hpp"

namespace hardcharger {

// Exit codes. Refused is the server's own answer for a credential that does not sign in;
// undecided means the check could not be made (the store unreachable or not initialized,
// a configuration problem) and says nothing about the credential.
constexpr int kVerifiedExit = 0;
constexpr int kRefusedExit = 1;
constexpr int kBadInputExit = 2;
constexpr int kUndecidedExit = 3;

// Reads the credential from `in`, writes the decision to `out`, returns the exit code.
int VerifyCredentials(const Config& config, std::istream& in, std::ostream& out);

}  // namespace hardcharger
