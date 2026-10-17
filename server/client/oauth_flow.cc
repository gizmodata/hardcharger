#include "oauth_flow.hpp"

#include <chrono>
#include <iostream>
#include <thread>

#include "platform.hpp"

namespace hardcharger::client {

namespace {
constexpr int kPollIntervalMs = 1000;
constexpr int kPollTimeoutSeconds = 300;  // 5 minutes

std::string Field(const boost::json::value& body, const char* key) {
  if (!body.is_object()) return {};
  auto* value = body.as_object().if_contains(key);
  return value != nullptr && value->is_string() ? std::string(value->as_string()) : std::string();
}
}  // namespace

arrow::Result<std::string> OAuthFlow::Authenticate(const std::string& oauth_base_url) {
  std::string base = oauth_base_url;
  while (!base.empty() && base.back() == '/') base.pop_back();
  std::string session_uuid;
  std::string auth_url;
  try {
    auto [status, body] = warpsql::client::GetJson(base + "/oauth/initiate", tls_);
    if (status != 200) {
      return arrow::Status::IOError("OAuth initiate failed with HTTP " + std::to_string(status) + ": " +
                                    (body.is_null() ? std::string() : boost::json::serialize(body)));
    }
    session_uuid = Field(body, "session_uuid");
    auth_url = Field(body, "auth_url");
  } catch (const warpsql::client::Error& e) {
    return arrow::Status::IOError("Failed to connect to the OAuth server at " + base + ": " + e.what());
  }
  if (session_uuid.empty() || auth_url.empty()) {
    return arrow::Status::IOError("OAuth initiate response missing required fields");
  }

  if (!OpenBrowser(auth_url)) {
    std::cerr << "Could not open browser automatically." << std::endl;
    std::cerr << "Please open this URL manually:" << std::endl;
    std::cerr << auth_url << std::endl;
  }
  std::cerr << "Waiting for browser login... (press Ctrl+C to cancel)" << std::endl;

  const auto start = std::chrono::steady_clock::now();
  const std::string poll_url = base + "/oauth/token/" + session_uuid;
  while (true) {
    if (std::chrono::steady_clock::now() - start >= std::chrono::seconds(kPollTimeoutSeconds)) {
      return arrow::Status::IOError("OAuth login timed out after " + std::to_string(kPollTimeoutSeconds) +
                                    " seconds");
    }
    int status = 0;
    boost::json::value body;
    try {
      std::tie(status, body) = warpsql::client::GetJson(poll_url, tls_);
    } catch (const warpsql::client::Error& e) {
      return arrow::Status::IOError(std::string("Failed to poll OAuth token: ") + e.what());
    }
    if (status == 404) return arrow::Status::IOError("OAuth sign-in expired or was already used");
    if (status != 200) return arrow::Status::IOError("OAuth token poll failed with HTTP " + std::to_string(status));
    const std::string state = Field(body, "status");
    if (state == "complete") {
      std::string token = Field(body, "token");
      if (token.empty()) return arrow::Status::IOError("OAuth response has status 'complete' but no token");
      return token;
    }
    if (state == "error") {
      std::string message = Field(body, "error");
      return arrow::Status::IOError("OAuth login failed: " + (message.empty() ? "Unknown OAuth error" : message));
    }
    if (state != "pending") return arrow::Status::IOError("Unknown OAuth poll status: " + state);
    std::this_thread::sleep_for(std::chrono::milliseconds(kPollIntervalMs));
  }
}

}  // namespace hardcharger::client
