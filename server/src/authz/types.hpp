// Shared vocabulary for Hardcharger role-based access control:
// privileges, object names and the case-folding rule every RBAC component uses.

#pragma once

#include <optional>
#include <string>
#include <vector>

namespace hardcharger::authz {

// Privileges are keyword phrases ("CREATE TABLE"), never underscored names, and
// are printed exactly as they are written in GRANT.
enum class Privilege {
  kSelect,
  kInsert,
  kUpdate,
  kDelete,
  kCreateTable,
  kCreateView,
  kCreateSchema,
  kDropTable,
  kDropView,
  kDropSchema,
  kAlterTable,
  kAlterView,
  kAll,  // every object privilege that applies to the target
};

// "SELECT", "CREATE TABLE", ...
std::string PrivilegeName(Privilege privilege);

// The object privileges ALL stands for on a table or view.
const std::vector<Privilege>& TableLevelPrivileges();

// Capabilities granted to roles with no object ON clause (GRANT READ FILESYSTEM TO etl).
// Each opens one family of statements that the admin command gate otherwise keeps for
// the administrator. The six storage privileges (locations.hpp) can also be granted on
// locations only: GRANT READ OBJECT STORAGE ON LOCATION 's3://bucket/in/' TO etl.
enum class SystemPrivilege {
  kManageCatalogs,      // ATTACH, DETACH
  kManageExtensions,    // INSTALL, LOAD, extension repositories
  kManageSecrets,       // CREATE / DROP SECRET, duckdb_secrets()
  kManageConfig,        // SET GLOBAL, instance-wide settings
  kReadFilesystem,      // read_csv / read_parquet / ..., FROM 'file', COPY FROM: local files
  kWriteFilesystem,     // COPY ... TO a local file
  kExportDatabase,      // EXPORT DATABASE
  kImportDatabase,      // IMPORT DATABASE
  kCheckpoint,          // CHECKPOINT, FORCE CHECKPOINT
  kReadObjectStorage,   // reading s3://, gs://, r2://, az://, abfss://
  kWriteObjectStorage,  // COPY ... TO object storage
  kReadHttp,            // reading http://, https://, hf://
  kWriteHttp,           // COPY ... TO http(s)
};

// "MANAGE CATALOGS", "READ FILESYSTEM", ...
std::string SystemPrivilegeName(SystemPrivilege privilege);
const std::vector<SystemPrivilege>& AllSystemPrivileges();
std::optional<SystemPrivilege> SystemPrivilegeFromName(const std::string& name);

// Does the setting hold a credential (http_proxy_password, s3_secret_access_key, ...),
// by its name? Reading one needs MANAGE CONFIG.
bool IsSensitiveSetting(const std::string& name);

// What a tenant's user is told about a catalog, schema, table or view it
// cannot see: exactly what it is told about one that does not exist.
std::string HiddenObjectMessage(const std::string& user_name);

// What a GRANT is attached to. kObject is one table or view; kSchema and
// kCatalog grant on every present and future object in the container.
enum class GrantScope { kObject, kSchema, kCatalog };

// The object privileges GRANT ALL stands for at a scope: every one that applies there.
// On a table or view the table-level ones; on a schema those and CREATE / DROP / ALTER
// TABLE and VIEW; on a catalog all of those and CREATE / DROP SCHEMA.
const std::vector<Privilege>& PrivilegesOfAll(GrantScope scope);

// One identifier, compared case-insensitively (DuckDB's rule for both quoted
// and unquoted identifiers) and stored as the user spelled it.
std::string FoldIdentifier(const std::string& identifier);
bool IdentifiersEqual(const std::string& a, const std::string& b);

// A catalog object: catalog, schema path (one element on DuckDB 1.x; nested
// schemas on 2.x) and object name. Empty schema_path / object_name mean "every"
// for schema- and catalog-scope grants.
struct ObjectPath {
  std::string catalog;
  std::vector<std::string> schema_path;
  std::string object_name;

  // `cat.sch.obj`, each part quoted when needed.
  std::string ToString() const;
};

// Quote an identifier for display only when it needs it.
std::string QuoteIdentifierIfNeeded(const std::string& identifier);

}  // namespace hardcharger::authz
