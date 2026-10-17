// An external policy service under access control: Open Policy Agent's data API
// (POST {"input": ...} -> {"result": true | false}). It is asked after a store user's
// statement passed Hardcharger's own check, and when their session starts, so it can
// only refuse more. Anything but {"result": true} refuses.
#pragma once

#include <boost/json.hpp>
#include <chrono>
#include <string>

namespace hardcharger::authz {

class PolicyService {
 public:
  enum class Decision { kAllow, kDeny, kNoAnswer };

  // url: the decision endpoint, e.g. http://127.0.0.1:8181/v1/data/hardcharger/allow
  // (http or https). Throws std::invalid_argument for a URL it cannot use.
  PolicyService(std::string url, std::chrono::milliseconds timeout);

  // Thread-safe; each call makes its own request.
  Decision Decide(const boost::json::object& input) const;
  const std::string& url() const { return url_; }

 private:
  std::string url_;
  bool tls_ = false;
  std::string host_;
  std::string port_;
  std::string target_;
  std::chrono::milliseconds timeout_;
};

}  // namespace hardcharger::authz
