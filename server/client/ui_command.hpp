// `hardcharger ui`: the web UI on its own, with no server behind it. It serves the same
// embedded UI the server serves at /ui, and the relay the UI reaches other Hardcharger
// servers through (src/web_relay.hpp), on a listener of its own; the UI then opens on
// Add a server, and everything it does goes to the servers the person adds.
#pragma once

namespace hardcharger::client {

// Runs the command with its arguments (argv[0] is "ui"); returns the process's exit code.
int RunUiCommand(int argc, char** argv);

}  // namespace hardcharger::client
