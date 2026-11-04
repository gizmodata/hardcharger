#include "ui_command.hpp"

#include <boost/asio/ip/address.hpp>
#include <boost/json.hpp>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>

#include "chargewire/backend.hpp"
#include "chargewire/server.hpp"
#include "chargewire/stop_signals.hpp"
#include "options.hpp"
#include "platform.hpp"
#include "version.hpp"
#include "web_relay.hpp"
#include "web_ui.hpp"

namespace hardcharger::client {

namespace {

// Nothing runs SQL here: the UI talks to the servers it adds, through the relay.
class NoEngine : public chargewire::Backend {
 public:
  chargewire::ServerIdentity Identity() override { return {"hardcharger-ui", hardcharger::kVersion}; }
  std::unique_ptr<chargewire::EngineSession> OpenSession(const chargewire::Principal&, const std::string&,
                                                         const boost::json::object&) override {
    throw chargewire::Error(chargewire::ErrorCode::kNotImplemented,
                            "this is the Hardcharger UI without a server: add a server in the UI");
  }
};

// Nobody signs in to the UI itself.
class NoSignIn : public chargewire::Authenticator {
 public:
  boost::json::array GrantTypes() override { return {}; }
};

struct Listen {
  std::string host = "127.0.0.1";
  uint16_t port = 0;
};

// "host:port", "[v6]:port" or ":port"; a bare number is a port on the loopback address.
std::optional<Listen> ParseListen(const std::string& text, std::string* error) {
  Listen listen;
  std::string host;
  std::string port;
  if (text.starts_with('[')) {
    const auto close = text.find(']');
    if (close == std::string::npos || close + 1 >= text.size() || text[close + 1] != ':') {
      *error = "--listen: write an IPv6 address as [address]:port";
      return std::nullopt;
    }
    host = text.substr(1, close - 1);
    port = text.substr(close + 2);
  } else if (const auto colon = text.rfind(':'); colon != std::string::npos) {
    host = text.substr(0, colon);
    port = text.substr(colon + 1);
  } else {
    port = text;
  }
  if (!host.empty()) listen.host = host;
  if (port.empty() || port.find_first_not_of("0123456789") != std::string::npos) {
    *error = port.empty() || port == text ? "--listen: no port (HOST:PORT; 0 picks a free one)"
                                          : "--listen: '" + port + "' is not a port";
    return std::nullopt;
  }
  try {
    const unsigned long value = std::stoul(port);
    if (value > 65535) throw std::out_of_range("port");
    listen.port = static_cast<uint16_t>(value);
  } catch (const std::exception&) {
    *error = "--listen: '" + port + "' is not a port";
    return std::nullopt;
  }
  return listen;
}

bool Loopback(const std::string& host) {
  boost::system::error_code ec;
  const auto address = boost::asio::ip::make_address(host, ec);
  if (ec) return host == "localhost";
  return address.is_loopback();
}

bool Readable(const std::string& path) { return std::ifstream(path).good(); }

}  // namespace

int RunUiCommand(int argc, char** argv) {
  std::string listen_text = "127.0.0.1:0";
  std::string tls_cert;
  std::string tls_key;
  bool no_open = false;
  bool allow_plaintext = false;
  bool help = false;
  bool quiet = false;
  CommandLine cl;
  cl.Section("Listener");
  cl.Value("listen", 0, "HOST:PORT",
           "Address and port to serve the UI on (default: 127.0.0.1:0, a free port on this computer only)",
           &listen_text);
  cl.Value("tls-cert", 0, "FILE", "Serve HTTPS with this certificate chain (PEM); needs --tls-key", &tls_cert);
  cl.Value("tls-key", 0, "FILE", "The certificate's private key (PEM)", &tls_key);
  cl.Flag("allow-plaintext", 0,
          "Serve plain HTTP on an address other computers can reach (the relay still uses TLS to the servers "
          "you add; this is about the UI's own page and your sign-ins to it)",
          &allow_plaintext);
  cl.Section("Browser");
  cl.Flag("no-open", 0, "Don't open the UI in the browser; just print its address", &no_open);
  cl.Section("Other");
  cl.Flag("quiet", 'q', "Print only the address", &quiet);
  cl.Flag("help", '?', "Show this help message", &help);
  std::string positional;
  if (auto error = cl.Parse(argc, argv, &positional)) {
    std::cerr << "Error: " << *error << std::endl;
    std::cerr << "Try 'hardcharger ui --help' for more information." << std::endl;
    return 1;
  }
  if (!positional.empty()) {
    std::cerr << "Error: hardcharger ui takes no positional argument (got '" << positional << "')" << std::endl;
    return 1;
  }
  if (help) {
    std::cout << "Usage: hardcharger ui [OPTIONS]\n\n"
                 "Serves the Hardcharger web UI on its own, with no server behind it: add the Hardcharger\n"
                 "servers you work with in the UI (the schema browser's Add server), and everything goes to\n"
                 "them through this program. Stop it with Ctrl+C.\n\n"
                 "Examples:\n"
                 "  hardcharger ui                          a free port on this computer, opens the browser\n"
                 "  hardcharger ui --listen 127.0.0.1:8080  a fixed port\n"
                 "  hardcharger ui --listen 0.0.0.0:8443 --tls-cert ui.pem --tls-key ui.key\n"
                 "                                          for others, over HTTPS\n"
              << std::endl;
    std::cout << "Hardcharger UI Options" << std::endl;
    cl.PrintHelp(std::cout);
    return 0;
  }
  if (hardcharger::EmbeddedWebUiFiles().empty()) {
    std::cerr << "Error: this hardcharger build has no web UI embedded (it was built without "
                 "-DHARDCHARGER_WEB_UI=<hardcharger-ui tarball>). Use a release build, or build the client "
                 "with the UI."
              << std::endl;
    return 2;
  }
  std::string error;
  const auto listen = ParseListen(listen_text, &error);
  if (!listen) {
    std::cerr << "Error: " << error << std::endl;
    return 1;
  }
  if (tls_cert.empty() != tls_key.empty()) {
    std::cerr << "Error: --tls-cert and --tls-key go together" << std::endl;
    return 1;
  }
  for (const auto& [what, path] : {std::pair{"--tls-cert", tls_cert}, std::pair{"--tls-key", tls_key}}) {
    if (!path.empty() && !Readable(path)) {
      std::cerr << "Error: " << what << ": cannot read " << path << std::endl;
      return 1;
    }
  }
  const bool tls = !tls_cert.empty();
  if (!tls && !Loopback(listen->host) && !allow_plaintext) {
    std::cerr << "Error: refusing to serve the UI in plain HTTP on " << listen->host
              << ", which other computers can reach: give it a certificate (--tls-cert and --tls-key), or "
                 "--allow-plaintext for a network you trust."
              << std::endl;
    return 1;
  }

  chargewire::StopSignals stop_signals;
  chargewire::ServerOptions options;
  options.host = listen->host;
  options.port = listen->port;
  if (tls) {
    chargewire::TlsOptions tls_options;
    tls_options.certificate_chain_file = tls_cert;
    tls_options.private_key_file = tls_key;
    options.tls = std::move(tls_options);
  }
  options.plain_handler = [](const chargewire::PlainRequest& request) {
    return hardcharger::ServeWebUi(hardcharger::EmbeddedWebUiFiles(), request);
  };
  hardcharger::web::RelayOptions relay_options;
  relay_options.local_server = false;
  relay_options.remote_servers = true;
  relay_options.version = hardcharger::kVersion;
  if (!quiet) {
    relay_options.log = [](bool warning, const std::string& message, const hardcharger::web::RelayLogFields& fields) {
      std::string line = (warning ? "warning: " : "") + message;
      for (const auto& [name, value] : fields) line += " " + name + "=" + value;
      std::cerr << line << std::endl;
    };
  }
  hardcharger::web::Relay relay(std::move(relay_options));
  options.app_endpoints = relay.Endpoints();

  chargewire::Server server(std::move(options), std::make_shared<NoEngine>(), std::make_shared<NoSignIn>());
  try {
    server.Start();
  } catch (const std::exception& e) {
    std::cerr << "Error: cannot listen on " << listen->host << ":" << listen->port << ": " << e.what() << std::endl;
    return 1;
  }
  const bool v6 = listen->host.find(':') != std::string::npos;
  const std::string shown = listen->host == "0.0.0.0" || listen->host == "::" ? "localhost"
                            : v6                                              ? "[" + listen->host + "]"
                                                                              : listen->host;
  const std::string url = std::string(tls ? "https" : "http") + "://" + shown + ":" + std::to_string(server.port()) + "/ui/";
  if (quiet) {
    std::cout << url << std::endl;
  } else {
    std::cout << "Hardcharger UI " << hardcharger::kVersion << " (no server: add servers in the UI)\n"
              << "  " << url << "\n"
              << "Press Ctrl+C to stop." << std::endl;
  }
  std::fflush(stdout);
  if (!no_open && !OpenBrowser(url) && !quiet) {
    std::cerr << "Could not open a browser: open " << url << " yourself." << std::endl;
  }
  stop_signals.Wait();
  if (!quiet) std::cerr << "Stopping the UI." << std::endl;
  server.Stop();
  server.Wait();
  return 0;
}

}  // namespace hardcharger::client
