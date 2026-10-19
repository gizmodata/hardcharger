// Browser sign-in (Warp SQL §4.3, the server's oauth_browser capability): the server
// gives a sign-in URL, which opens in a browser ($HARDCHARGER_BROWSER, else the system's),
// and the client polls the server until the person has signed in with the identity
// provider, then holds Hardcharger's access token.
#pragma once

#include <arrow/status.h>

#include "warpsql/client.hpp"

namespace hardcharger::client {

// Signs `client` in through the browser. `discovery` is the server's GET /warp.
arrow::Status SignInWithBrowser(warpsql::client::Client& client, const warpsql::client::Discovery& discovery);

}  // namespace hardcharger::client
