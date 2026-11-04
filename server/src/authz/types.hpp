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
  kCreateMacro,  // scalar and table macros, ON SCHEMA or ON CATALOG
  kDropMacro,    // dropping or replacing one, ON SCHEMA or ON CATALOG
  kCreateType,   // enums, structs and aliases, ON SCHEMA or ON CATALOG
  kDropType,     // dropping or replacing one, ON SCHEMA or ON CATALOG
  kCreateTrigger,  // a trigger on a table the role owns, ON SCHEMA or ON CATALOG
  kDropTrigger,    // dropping or replacing one not its own, ON SCHEMA or ON CATALOG
  kUsage,           // nextval() / currval() of a sequence: ON SEQUENCE, ON SCHEMA or ON CATALOG
  kCreateSequence,  // ON SCHEMA or ON CATALOG
  kDropSequence,    // dropping one, or setval() of one: ON SCHEMA or ON CATALOG
  kMaintain,        // ANALYZE and VACUUM of a table: ON TABLE, ON SCHEMA or ON CATALOG
  kExecute,         // calling a table function: ON TABLE FUNCTION name (never part of ALL)
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
// On a table or view the table-level ones and MAINTAIN; on a schema those, CREATE / DROP / ALTER
// TABLE and VIEW, CREATE / DROP MACRO, TYPE and TRIGGER, USAGE, CREATE and DROP SEQUENCE,
// and CREATE / DROP SCHEMA (on a schema: its child schemas); on a catalog all of those.
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

  // `cat.sch.obj`, each part quoted when needed. A table function (no catalog, no
  // schema) is its name alone.
  std::string ToString() const;
  // A table function's path: no catalog, no schema, the function's name.
  bool IsTableFunction() const { return catalog.empty() && schema_path.empty() && !object_name.empty(); }
};

// Quote an identifier for display only when it needs it.
std::string QuoteIdentifierIfNeeded(const std::string& identifier);

// An argument of a table function that names the files it reads, declared with the
// EXECUTE grant (GRANT EXECUTE ON TABLE FUNCTION read_hl7 (PATH ARGUMENT 2) TO ...): the
// call's value there goes through the storage privileges. By position (1-based) or by
// a named parameter's name. Stored in the grant's column list as "argument:2" /
// "parameter:files".
struct PathArgument {
  int position = 0;       // 0: a named parameter
  std::string parameter;  // empty: a positional argument
  // "argument 2" / "parameter files"
  std::string Text() const;
  std::string Stored() const;
  static std::optional<PathArgument> FromStored(const std::string& stored);
};

// A schema path as one name: its parts joined with dots, a part written quoted when it
// contains a dot or a double quote (so the nested `sales.payroll` and a top-level schema
// named "sales.payroll" read differently, and the text goes into SQL between the catalog
// and the table as it is). Chargewire metadata names schemas the same way.
std::string SchemaPathText(const std::vector<std::string>& path);
// The parts of a schema name written that way (the inverse of SchemaPathText).
std::vector<std::string> ParseSchemaPathText(const std::string& text);

}  // namespace hardcharger::authz
