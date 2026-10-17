#include "authz/opa.hpp"

#include <openssl/ssl.h>

#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/url/parse.hpp>
#include <optional>
#include <stdexcept>

namespace hardcharger::authz {

namespace {

namespace beast = boost::beast;
namespace http = beast::http;
namespace net = boost::asio;
using tcp = net::ip::tcp;

using Response = http::response<http::string_body>;

// One POST over `stream`.
template <class Stream>
net::awaitable<Response> Post(Stream& stream, const std::string& host, const std::string& target,
                              const std::string& body) {
  http::request<http::string_body> request{http::verb::post, target, 11};
  request.set(http::field::host, host);
  request.set(http::field::content_type, "application/json");
  request.body() = body;
  request.prepare_payload();
  co_await http::async_write(stream, request, net::use_awaitable);
  beast::flat_buffer buffer;
  Response response;
  co_await http::async_read(stream, buffer, response, net::use_awaitable);
  co_return response;
}

}  // namespace

PolicyService::PolicyService(std::string url, std::chrono::milliseconds timeout)
    : url_(std::move(url)), timeout_(timeout) {
  auto parsed = boost::urls::parse_uri(url_);
  if (!parsed || (parsed->scheme() != "http" && parsed->scheme() != "https") ||
      parsed->host().empty()) {
    throw std::invalid_argument("rbac_opa_url must be an http:// or https:// URL");
  }
  tls_ = parsed->scheme() == "https";
  host_ = std::string(parsed->host());
  port_ = parsed->has_port() ? std::string(parsed->port()) : (tls_ ? "443" : "80");
  target_ = parsed->encoded_target().empty() ? "/" : std::string(parsed->encoded_target());
}

PolicyService::Decision PolicyService::Decide(const boost::json::object& input) const {
  try {
    const std::string body = boost::json::serialize(boost::json::object{{"input", input}});
    // The whole exchange (resolve, connect, TLS, request, reply) gets one deadline:
    // the io_context runs for at most the timeout, and an unfinished exchange is
    // no answer. (Beast's own timeouts only cover asynchronous operations.)
    // Declared before the io_context: an unfinished exchange's streams, destroyed with
    // it, still refer to the TLS context.
    net::ssl::context ssl(net::ssl::context::tls_client);
    std::optional<Response> response;
    net::io_context io;
    auto exchange = [&]() -> net::awaitable<void> {
      tcp::resolver resolver(io);
      const auto endpoints = co_await resolver.async_resolve(host_, port_, net::use_awaitable);
      if (tls_) {
        ssl.set_default_verify_paths();
        ssl.set_verify_mode(net::ssl::verify_peer);
        beast::ssl_stream<beast::tcp_stream> stream(io, ssl);
        if (!SSL_set_tlsext_host_name(stream.native_handle(), host_.c_str())) co_return;
        stream.set_verify_callback(net::ssl::host_name_verification(host_));
        co_await beast::get_lowest_layer(stream).async_connect(endpoints, net::use_awaitable);
        co_await stream.async_handshake(net::ssl::stream_base::client, net::use_awaitable);
        response = co_await Post(stream, host_, target_, body);
      } else {
        beast::tcp_stream stream(io);
        co_await stream.async_connect(endpoints, net::use_awaitable);
        response = co_await Post(stream, host_, target_, body);
      }
    };
    net::co_spawn(io, exchange(), [](std::exception_ptr) {});
    io.run_for(timeout_);
    if (!response || response->result() != http::status::ok) return Decision::kNoAnswer;
    boost::system::error_code parse_error;
    const auto reply = boost::json::parse(response->body(), parse_error);
    if (parse_error || !reply.is_object()) return Decision::kNoAnswer;
    const auto* result = reply.as_object().if_contains("result");
    if (result == nullptr || !result->is_bool()) return Decision::kNoAnswer;
    return result->as_bool() ? Decision::kAllow : Decision::kDeny;
  } catch (...) {
    return Decision::kNoAnswer;
  }
}

}  // namespace hardcharger::authz
