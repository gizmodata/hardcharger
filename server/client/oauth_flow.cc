#include "oauth_flow.hpp"

#include <chrono>
#include <iostream>

#include "platform.hpp"

namespace hardcharger::client {

namespace {
constexpr auto kTimeout = std::chrono::seconds(300);
}  // namespace

arrow::Status SignInWithBrowser(chargewire::client::Client& client, const chargewire::client::Discovery& discovery) {
  if (!discovery.Has("oauth_browser")) {
    return arrow::Status::IOError(
        "The server does not offer browser sign-in (capability oauth_browser): its administrator turns it on "
        "with oauth_client_id and the identity provider's settings");
  }
  const std::string provider = chargewire::client::Client::BrowserSignInProviderName(discovery);
  try {
    client.SignInWithBrowser(
        [&](const std::string& url) {
          if (!OpenBrowser(url)) {
            std::cerr << "Could not open a browser. Open this URL to sign in:" << std::endl << url << std::endl;
          }
          std::cerr << "Waiting for the browser sign-in" << (provider.empty() ? "" : " with " + provider)
                    << "... (press Ctrl+C to cancel)" << std::endl;
        },
        kTimeout);
  } catch (const chargewire::client::Error& e) {
    if (e.code() == "timeout") {
      return arrow::Status::IOError("The browser sign-in was not completed within ", kTimeout.count(), " seconds");
    }
    if (e.code() == "flow_not_found") return arrow::Status::IOError("The browser sign-in expired; try again");
    return arrow::Status::IOError("Browser sign-in failed (", e.code(), "): ", e.detail());
  }
  return arrow::Status::OK();
}

}  // namespace hardcharger::client
