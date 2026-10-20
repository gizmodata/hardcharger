// Persistent store for users, roles, memberships, grants and owners.
//
// The store lives in PostgreSQL, so every replica of a cluster reads the same
// grants, or, for development and a server that will only ever run as one replica,
// in a DuckDB file. It is reached through a PRIVATE in-process DuckDB instance
// (separate from the one serving clients) that attaches the store database: no client
// session can see or query it. A DuckDB file is also kept out of reach of the serving
// instance's SQL (file_path(); see PrivateFiles in guards.hpp), as DuckDB's file lock
// does not stop the same process from opening it a second time. Unit tests use an
// in-memory store.
//
// Every mutation runs in one store transaction that first bumps a data version
// row, which serializes writers across replicas; readers poll that version to
// know when to reload.
//
// Every store round trip is bounded (StoreOptions::timeout): a store that stops answering
// (PostgreSQL paused, overloaded, or unreachable without a reset) fails the call with
// "access control store unavailable: timed out after N s" instead of holding it. The
// store's errors never carry its connection string's credentials.

#pragma once

#include <arrow/result.h>
#include <arrow/status.h>

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "authz/model.hpp"

namespace duckdb {
class DuckDB;
class Connection;
}  // namespace duckdb

namespace hardcharger::authz {

class StoreTransaction;
class StoreLink;

// The version of the store's schema this server writes and reads. A store whose schema
// is newer (a later server migrated it) is refused at startup.
constexpr int64_t kStoreSchemaVersion = 1;

struct StoreOptions {
  // A PostgreSQL connection string or URI ("postgresql://...", "postgres://...", or libpq
  // "host=... dbname=..."), a DuckDB file ("duckdb:<path>", or a path ending in .duckdb),
  // or "memory" for an in-process store (tests).
  std::string uri;
  // The longest one store round trip may take (rbac_store_timeout_seconds). For
  // PostgreSQL it also sets libpq's connect_timeout, TCP keepalives, tcp_user_timeout and
  // the session's statement_timeout, each unless the connection string sets it.
  std::chrono::milliseconds timeout{10000};
  // This server's cluster (cluster_id), or empty. The store records the first cluster
  // that opens it and refuses another one, unless `shared`.
  std::string cluster_id;
  bool shared = false;  // rbac_store_shared: several clusters share users, roles and grants
};

// The text "access control store unavailable" every error of an unreachable or hung
// store carries.
inline constexpr const char* kStoreUnavailable = "access control store unavailable";

// Whether `status` says the store did not answer (timed out or unreachable), as opposed
// to refusing a statement.
bool IsStoreUnavailable(const arrow::Status& status);

// A PostgreSQL store's connection string as the store connects with it: with libpq's
// connect_timeout, keepalives, tcp_user_timeout and statement_timeout (from `timeout`)
// added, each unless `uri` sets it. (Exposed for the unit tests.)
std::string StoreConnectionWithTimeouts(const std::string& uri, std::chrono::milliseconds timeout);

class RbacStore {
 public:
  static arrow::Result<std::unique_ptr<RbacStore>> Open(const StoreOptions& options);
  static arrow::Result<std::unique_ptr<RbacStore>> Open(const std::string& uri) {
    StoreOptions options;
    options.uri = uri;
    return Open(options);
  }

  // The file a DuckDB store URI names, as given (empty: not a DuckDB store).
  static std::string DuckDBFile(const std::string& uri);
  ~RbacStore();

  // Read everything into a snapshot.
  arrow::Result<std::shared_ptr<const Snapshot>> Load();

  // The store's data version (cheap; replicas poll it).
  arrow::Result<int64_t> CurrentVersion();

  // Run `fn` in one store transaction. The data version is bumped first (so
  // concurrent writers on any replica serialize), `fn` receives a snapshot read
  // inside the transaction and a handle to write with. Commits when `fn`
  // returns OK; rolls back otherwise. A serialization conflict with another
  // replica is retried.
  arrow::Status Mutate(const std::function<arrow::Status(StoreTransaction&, const Snapshot&)>& fn);

  const std::string& description() const { return description_; }
  // A DuckDB store's file, absolute; empty for any other store.
  const std::string& file_path() const { return file_path_; }
  // The cluster the store belongs to (its first cluster_id), or empty.
  const std::string& cluster_id() const { return cluster_id_; }
  std::chrono::milliseconds timeout() const { return options_.timeout; }

  // `text` without this store's connection string or any credential it may quote.
  std::string Redact(const std::string& text) const;

 private:
  RbacStore() = default;
  arrow::Status OpenStore();
  arrow::Status InitializeSchema();
  arrow::Status CheckOwnership();
  void CheckpointFile();
  arrow::Result<std::shared_ptr<const Snapshot>> LoadLocked();
  // Takes mutex_ within the timeout, or says the store is busy with a call that hangs.
  arrow::Status Lock(std::unique_lock<std::timed_mutex>& lock);
  arrow::Status Redacted(const arrow::Status& status) const;
  template <typename T>
  arrow::Result<T> Redacted(arrow::Result<T> result) const {
    if (result.ok()) return result;
    return Redacted(result.status());
  }

  StoreOptions options_;
  std::string connect_uri_;  // options_.uri with the timeouts libpq takes added
  std::shared_ptr<StoreLink> link_;
  std::timed_mutex mutex_;
  std::string description_;  // for logs; never contains credentials
  std::string secret_dir_;   // the private instance's (empty) secret directory
  std::string file_path_;    // a DuckDB store's file
  std::string cluster_id_;

  friend class StoreTransaction;
};

// Writes inside RbacStore::Mutate. Only valid inside the callback.
class StoreTransaction {
 public:
  arrow::Status InsertUser(const UserRecord& user);
  arrow::Status UpdateUser(const UserRecord& user);
  arrow::Status DeleteUser(const std::string& user_id);
  arrow::Status InsertRole(const RoleRecord& role);
  arrow::Status DeleteRole(const std::string& role_id);
  arrow::Status InsertMembership(const MembershipRecord& membership);
  arrow::Status DeleteMembership(PrincipalKind member_kind, const std::string& member_id,
                                 const std::string& role_id);
  arrow::Status InsertGrant(const ObjectGrantRecord& grant);
  arrow::Status UpdateGrant(const ObjectGrantRecord& grant);
  arrow::Status DeleteGrant(const std::string& grant_id);
  arrow::Status UpsertOwner(const OwnerRecord& owner);
  // nullopt removes the attribute.
  arrow::Status SetUserAttribute(const std::string& user_id, const std::string& attribute,
                                 const std::optional<std::string>& value);
  arrow::Status UpsertTenant(const TenantRecord& tenant);
  // Also forgets the tenant's catalog and principal assignments.
  arrow::Status DeleteTenant(const std::string& tenant_id);
  // An empty tenant_id removes the assignment.
  arrow::Status SetPrincipalTenant(PrincipalKind kind, const std::string& id,
                                   const std::string& tenant_id);
  arrow::Status SetCatalogTenant(const std::string& catalog, const std::string& tenant_id);
  arrow::Status InsertGroupMapping(const GroupMappingRecord& mapping);
  arrow::Status InsertScopedToken(const ScopedTokenRecord& token);
  arrow::Status DeleteScopedToken(const std::string& token_id);
  arrow::Status DeleteGroupMapping(const std::string& mapping_id);
  arrow::Status UpsertRowPolicy(const RowPolicyRecord& policy);
  arrow::Status DeleteRowPolicy(const std::string& policy_id);
  arrow::Status UpsertMaskingPolicy(const MaskingPolicyRecord& policy);
  // Also removes the policy's column bindings.
  arrow::Status DeleteMaskingPolicy(const std::string& policy_id);
  arrow::Status SetMaskBinding(const MaskBindingRecord& binding, bool remove);
  // One record per (role, privilege, grantor).
  arrow::Status UpsertSystemPrivilege(const SystemPrivilegeRecord& record);
  arrow::Status DeleteSystemPrivilege(const SystemPrivilegeRecord& record);
  arrow::Status DeleteOwner(const ObjectPath& object);

  // A fresh random id (UUID v4 text).
  static std::string NewId();

  // The store as this transaction sees it, its own writes included.
  arrow::Result<std::shared_ptr<const Snapshot>> Reload();

 private:
  StoreTransaction(RbacStore& store, StoreLink& conn) : store_(store), conn_(conn) {}
  RbacStore& store_;
  StoreLink& conn_;
  friend class RbacStore;
};

}  // namespace hardcharger::authz
