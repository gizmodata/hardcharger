#include "plain_http_probe.hpp"

#include <openssl/bio.h>
#include <openssl/ssl.h>

#include <array>
#include <memory>
#include <string_view>

#include <boost/asio/connect.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/address.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/read.hpp>
#include <boost/asio/write.hpp>

namespace hardcharger::client {

namespace {

namespace asio = boost::asio;
using tcp = asio::ip::tcp;

// A real ClientHello, as OpenSSL writes it for this host, without a socket.
std::string ClientHello(const std::string& host) {
  std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)> ctx(SSL_CTX_new(TLS_client_method()), SSL_CTX_free);
  if (!ctx) return {};
  std::unique_ptr<SSL, decltype(&SSL_free)> ssl(SSL_new(ctx.get()), SSL_free);
  if (!ssl) return {};
  BIO* in = BIO_new(BIO_s_mem());
  BIO* out = BIO_new(BIO_s_mem());
  if (in == nullptr || out == nullptr) {
    BIO_free(in);
    BIO_free(out);
    return {};
  }
  SSL_set_bio(ssl.get(), in, out);  // ssl owns both now
  boost::system::error_code ec;
  asio::ip::make_address(host, ec);
  if (ec) SSL_set_tlsext_host_name(ssl.get(), host.c_str());  // SNI for names, not addresses
  SSL_set_connect_state(ssl.get());
  SSL_do_handshake(ssl.get());  // writes the ClientHello, then wants to read
  char* data = nullptr;
  const long size = BIO_get_mem_data(out, &data);
  return size > 0 ? std::string(data, static_cast<size_t>(size)) : std::string();
}

}  // namespace

bool AnswersTlsWithHttp(const std::string& host, int port, std::chrono::milliseconds timeout) {
  const std::string hello = ClientHello(host);
  if (hello.empty()) return false;
  asio::io_context io;
  tcp::resolver resolver(io);
  tcp::socket socket(io);
  std::array<char, 5> head{};
  size_t got = 0;
  bool ok = false;
  resolver.async_resolve(host, std::to_string(port), [&](boost::system::error_code ec, tcp::resolver::results_type hosts) {
    if (ec) return;
    asio::async_connect(socket, hosts, [&](boost::system::error_code ec, const tcp::endpoint&) {
      if (ec) return;
      asio::async_write(socket, asio::buffer(hello), [&](boost::system::error_code ec, size_t) {
        if (ec) return;
        asio::async_read(socket, asio::buffer(head), [&](boost::system::error_code, size_t n) {
          got = n;
          ok = true;
        });
      });
    });
  });
  io.run_for(timeout);
  boost::system::error_code ignored;
  socket.close(ignored);
  return ok && got == head.size() && std::string_view(head.data(), head.size()) == "HTTP/";
}

}  // namespace hardcharger::client
