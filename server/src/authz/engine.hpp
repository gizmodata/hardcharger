// The access-control engine: runs CREATE USER / ROLE, GRANT, REVOKE, ... against
// the store, keeps the current snapshot (reloading when another replica changes
// the store), authenticates store users, and answers authorization questions.

#pragma once

#include <arrow/result.h>
#include <arrow/status.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "authz/ddl.hpp"
#include "authz/model.hpp"
#include "authz/opa.hpp"
#include "authz/password.hpp"
#include "authz/store.hpp"

namespace hardcharger::authz {

// A table or view as the engine (DuckDB) resolved it.
struct ResolvedObject {
  ObjectPath path;
  ObjectType type = ObjectType::kTable;
  std::vector<std::string> columns;
  std::string definition_hash;  // views: see OwnerRecord::definition_hash
};

// Resolves names in GRANT / REVOKE / ALTER ... OWNER TO the way the caller's
// session would, so a grant names exactly the object a query would read.
class ObjectResolver {
 public:
  virtual ~ObjectResolver() = default;
  virtual arrow::Result<ResolvedObject> ResolveObject(const QualifiedName& name) = 0;
  // catalog.schema[.sub...] or schema (in the session's catalog)
  virtual arrow::Result<ObjectPath> ResolveSchema(const QualifiedName& name) = 0;
  virtual arrow::Result<std::string> ResolveCatalog(const std::string& name) = 0;

  // Policy SQL tried against the real table before it is stored (a dry-run
  // bind). The defaults accept; Hardcharger's DuckDB resolver checks.
  virtual arrow::Status CheckRowPredicate(const ResolvedObject& table,
                                          const std::string& predicate) {
    return arrow::Status::OK();
  }
  virtual arrow::Status CheckMaskingPolicy(const MaskingPolicyRecord& policy) {
    return arrow::Status::OK();
  }
  virtual arrow::Status CheckMaskBinding(const ResolvedObject& table, const std::string& column,
                                         const MaskingPolicyRecord& policy) {
    return arrow::Status::OK();
  }
};

// The result of an access-control statement, returned to the client as a table.
struct CommandResult {
  std::vector<std::string> column_names;
  std::vector<std::vector<std::string>> rows;
  // Users whose sessions must end now (ALTER USER ... DISABLE, DROP USER).
  std::vector<std::string> terminate_user_ids;
  // SET ROLE: the session's new active role (empty: all roles).
  std::optional<std::string> active_role_id;
  // CREATE SCOPED TOKEN: the token to sign and hand back (a "token" column).
  struct MintedToken {
    std::string token_id;
    std::string user_name;
    int64_t expires_at = 0;
  };
  std::optional<MintedToken> minted;
};

class RbacEngine {
 public:
  struct Options {
    std::string store_uri;
    int password_iterations = kDefaultPasswordIterations;
    // How often to look for changes made by other replicas. 0 disables polling.
    std::chrono::milliseconds poll_interval{1000};
    // The server administrator's user name, which no store user may take.
    std::string administrator_name;
    // Client certificate identities that sign in as the administrator (mtls_admin_identities),
    // which no store user may take either.
    std::vector<std::string> certificate_administrators;
    // An external policy service (Open Policy Agent decision URL) asked after
    // Hardcharger's own check; empty: none.
    std::string policy_service_url;
    std::chrono::milliseconds policy_service_timeout{2000};
    // The longest one store round trip may take (rbac_store_timeout_seconds).
    std::chrono::milliseconds store_timeout{10000};
    // This server's cluster_id, which the store must belong to unless store_shared.
    std::string cluster_id;
    bool store_shared = false;
    // How old the snapshot may grow while the store does not answer before store users
    // are refused (rbac_max_snapshot_age_seconds); 0: no limit.
    std::chrono::seconds max_snapshot_age{0};
  };

  static arrow::Result<std::shared_ptr<RbacEngine>> Create(const Options& options);
  ~RbacEngine();

  // Stops the store poller and joins it (idempotent; the destructor calls it too). The
  // server calls it on its way out, before DuckDB goes away: something may still hold the
  // engine then (a session's connection state), and a poll running past main() would
  // query a DuckDB whose statics the process has freed.
  void Stop();

  RbacEngine(const RbacEngine&) = delete;
  RbacEngine& operator=(const RbacEngine&) = delete;

  // The external policy service, or null.
  const PolicyService* policy_service() const { return policy_service_.get(); }

  // The snapshot every check reads. Never null.
  std::shared_ptr<const Snapshot> snapshot() const;

  // Reload when the store's version moved (another replica wrote).
  arrow::Status Refresh();

  arrow::Result<CommandResult> Execute(const RbacCommand& command, const Principal& actor,
                                       ObjectResolver& resolver);

  // Password login for a store user. Returns the principal on success; any
  // failure (unknown user, wrong password, disabled) is the same error.
  arrow::Result<Principal> AuthenticatePassword(const std::string& user_name,
                                                const std::string& password) const;

  // The principal for a user identified by a token. Fails unless the user
  // exists in the store and is enabled.
  arrow::Result<Principal> PrincipalForUser(const std::string& user_name) const;

  // Is the principal still allowed to run anything (exists, enabled)?
  bool IsActive(const Principal& principal) const;

  // Ownership bookkeeping for objects created / dropped through Hardcharger.
  arrow::Status RecordOwner(const ObjectPath& object, ObjectType type, const Principal& owner,
                            const std::string& definition_hash = "");
  arrow::Status ForgetOwner(const ObjectPath& object);
  // Puts an owner record back as it was.
  arrow::Status PutOwner(const OwnerRecord& record);

  // Seconds since the store last answered (the snapshot is known current up to then).
  double SnapshotAgeSeconds() const;
  // Whether the store's last poll answered.
  bool store_up() const { return store_up_.load(); }
  // Fails (permission denied, saying why) when the store has not answered for longer
  // than max_snapshot_age: store users are refused until it does. Not for the
  // administrator, which the caller leaves out.
  arrow::Status RequireFreshSnapshot() const;
  // The store's cluster (its first cluster_id), or empty.
  const std::string& store_cluster_id() const { return store_->cluster_id(); }

  const std::string& store_description() const { return store_->description(); }
  const std::string& store_file() const { return store_->file_path(); }

  // Called (outside any engine lock) whenever a newer snapshot replaces the
  // current one: after a local change or when the version poll sees another
  // node's. Each node uses it to end its own sessions of users who were
  // disabled, dropped or had their password changed anywhere in the cluster.
  using SnapshotListener = std::function<void(const Snapshot&)>;
  void SetSnapshotListener(SnapshotListener listener);

 private:
  arrow::Result<CommandResult> ExecuteScopedToken(const ScopedTokenCommand& command,
                                                  const Principal& actor);
  explicit RbacEngine(Options options) : options_(std::move(options)) {}
  void PollLoop();
  void Publish(std::shared_ptr<const Snapshot> snapshot);
  // Notes whether a poll of the store answered; logs an outage (rate-limited) and its end.
  void NoteStoreAnswer(const arrow::Status& status);

  Options options_;
  std::string dummy_hash_;  // verified against for unknown users (equal timing)
  std::unique_ptr<RbacStore> store_;
  std::unique_ptr<PolicyService> policy_service_;
  mutable std::mutex snapshot_mutex_;
  std::shared_ptr<const Snapshot> snapshot_;
  std::mutex listener_mutex_;
  SnapshotListener listener_;

  std::atomic<int64_t> last_answer_ns_{0};  // steady clock: the store last answered
  std::atomic<bool> store_up_{true};
  int64_t outage_started_ns_ = 0;  // the poller's: when the store stopped answering
  int64_t outage_logged_ns_ = 0;   // ... and when that was last logged

  std::mutex poll_mutex_;
  std::condition_variable poll_cv_;
  bool stopping_ = false;  // guarded by poll_mutex_
  std::mutex stop_mutex_;  // serializes Stop()'s join
  std::thread poller_;
};

// "Hardcharger access control: ..." — the prefix every RBAC error carries.
arrow::Status AccessControlError(const std::string& message);

}  // namespace hardcharger::authz
