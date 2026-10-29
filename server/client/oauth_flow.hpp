// Browser sign-in (Chargewire §4.3, the server's oauth_browser capability): the server
// gives a sign-in URL, which opens in a browser ($HARDCHARGER_BROWSER, else the system's),
// and the client polls the server until the person has signed in with the identity
// provider, then holds Hardcharger's access token.
#pragma once

#include <arrow/status.h>

#include "chargewire/client.hpp"

namespace hardcharger::client {

// Signs `client` in through the browser. `discovery` is the server's GET /chargewire.
arrow::Status SignInWithBrowser(chargewire::client::Client& client, const chargewire::client::Discovery& discovery);

}  // namespace hardcharger::client
