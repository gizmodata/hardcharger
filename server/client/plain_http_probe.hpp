// Tells a TLS handshake that met a plain-HTTP server from any other TLS failure.
#pragma once

#include <chrono>
#include <string>

namespace hardcharger::client {

// Whether the server at host:port answers a TLS ClientHello with an HTTP response (the bytes
// "HTTP/" where a ServerHello belongs). It sends a ClientHello and nothing else: no request, no
// credential, nothing in plaintext. False when it can't tell (refused, timed out, closed, a
// TLS answer).
bool AnswersTlsWithHttp(const std::string& host, int port,
                        std::chrono::milliseconds timeout = std::chrono::milliseconds(5000));

}  // namespace hardcharger::client
