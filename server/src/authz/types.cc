#include "authz/types.hpp"

#include <cctype>

namespace hardcharger::authz {

std::string PrivilegeName(Privilege privilege) {
  switch (privilege) {
    case Privilege::kSelect:
      return "SELECT";
    case Privilege::kInsert:
      return "INSERT";
    case Privilege::kUpdate:
      return "UPDATE";
    case Privilege::kDelete:
      return "DELETE";
    case Privilege::kCreateTable:
      return "CREATE TABLE";
    case Privilege::kCreateView:
      return "CREATE VIEW";
    case Privilege::kCreateSchema:
      return "CREATE SCHEMA";
    case Privilege::kDropTable:
      return "DROP TABLE";
    case Privilege::kDropView:
      return "DROP VIEW";
    case Privilege::kDropSchema:
      return "DROP SCHEMA";
    case Privilege::kAlterTable:
      return "ALTER TABLE";
    case Privilege::kAlterView:
      return "ALTER VIEW";
    case Privilege::kCreateMacro:
      return "CREATE MACRO";
    case Privilege::kDropMacro:
      return "DROP MACRO";
    case Privilege::kCreateType:
      return "CREATE TYPE";
    case Privilege::kDropType:
      return "DROP TYPE";
    case Privilege::kCreateTrigger:
      return "CREATE TRIGGER";
    case Privilege::kDropTrigger:
      return "DROP TRIGGER";
    case Privilege::kUsage:
      return "USAGE";
    case Privilege::kCreateSequence:
      return "CREATE SEQUENCE";
    case Privilege::kDropSequence:
      return "DROP SEQUENCE";
    case Privilege::kMaintain:
      return "MAINTAIN";
    case Privilege::kExecute:
      return "EXECUTE";
    case Privilege::kAll:
      return "ALL";
  }
  return "UNKNOWN";
}

const std::vector<Privilege>& TableLevelPrivileges() {
  static const std::vector<Privilege> kPrivileges = {Privilege::kSelect, Privilege::kInsert,
                                                     Privilege::kUpdate, Privilege::kDelete};
  return kPrivileges;
}

const std::vector<Privilege>& PrivilegesOfAll(GrantScope scope) {
  static const std::vector<Privilege> kObject = {Privilege::kSelect, Privilege::kInsert, Privilege::kUpdate,
                                                 Privilege::kDelete, Privilege::kMaintain};
  static const std::vector<Privilege> kSchema = {
      Privilege::kSelect,      Privilege::kInsert,     Privilege::kUpdate,    Privilege::kDelete,
      Privilege::kCreateTable, Privilege::kCreateView, Privilege::kDropTable, Privilege::kDropView,
      Privilege::kAlterTable,  Privilege::kAlterView,  Privilege::kCreateMacro, Privilege::kDropMacro,
      Privilege::kCreateType,  Privilege::kDropType,   Privilege::kCreateTrigger, Privilege::kDropTrigger,
      Privilege::kUsage,       Privilege::kCreateSequence, Privilege::kDropSequence, Privilege::kMaintain,
      Privilege::kCreateSchema, Privilege::kDropSchema};  // on a schema: its child schemas
  static const std::vector<Privilege> kCatalog = kSchema;
  switch (scope) {
    case GrantScope::kObject:
      return kObject;
    case GrantScope::kSchema:
      return kSchema;
    case GrantScope::kCatalog:
      return kCatalog;
  }
  return TableLevelPrivileges();
}

// DuckDB compares identifiers case-insensitively by ASCII lowercasing, whether
// or not they were quoted; anything outside ASCII compares byte for byte.
std::string FoldIdentifier(const std::string& identifier) {
  std::string folded = identifier;
  for (auto& c : folded) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return folded;
}

bool IdentifiersEqual(const std::string& a, const std::string& b) {
  return FoldIdentifier(a) == FoldIdentifier(b);
}

std::string QuoteIdentifierIfNeeded(const std::string& identifier) {
  bool plain = !identifier.empty() &&
               (std::isalpha(static_cast<unsigned char>(identifier[0])) || identifier[0] == '_');
  for (char c : identifier) {
    if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_')) plain = false;
  }
  if (plain) return identifier;
  std::string quoted = "\"";
  for (char c : identifier) {
    if (c == '"') quoted += '"';
    quoted += c;
  }
  return quoted + "\"";
}

std::string PathArgument::Text() const {
  return position > 0 ? "argument " + std::to_string(position) : "parameter " + parameter;
}

std::string PathArgument::Stored() const {
  return position > 0 ? "argument:" + std::to_string(position) : "parameter:" + parameter;
}

std::optional<PathArgument> PathArgument::FromStored(const std::string& stored) {
  PathArgument argument;
  if (stored.starts_with("argument:")) {
    try {
      argument.position = std::stoi(stored.substr(9));
    } catch (const std::exception&) {
      return std::nullopt;
    }
    return argument.position > 0 ? std::optional(argument) : std::nullopt;
  }
  if (stored.starts_with("parameter:") && stored.size() > 10) {
    argument.parameter = stored.substr(10);
    return argument;
  }
  return std::nullopt;
}

std::string SchemaPathText(const std::vector<std::string>& path) {
  std::string out;
  for (const auto& part : path) {
    if (!out.empty()) out += ".";
    if (part.find('.') == std::string::npos && part.find('"') == std::string::npos) {
      out += part;
      continue;
    }
    out += '"';
    for (char c : part) {
      if (c == '"') out += '"';
      out += c;
    }
    out += '"';
  }
  return out;
}

std::vector<std::string> ParseSchemaPathText(const std::string& text) {
  std::vector<std::string> parts;
  std::string part;
  bool quoted = false;
  for (size_t i = 0; i < text.size(); ++i) {
    const char c = text[i];
    if (quoted) {
      if (c == '"' && i + 1 < text.size() && text[i + 1] == '"') {
        part += '"';
        ++i;
      } else if (c == '"') {
        quoted = false;
      } else {
        part += c;
      }
    } else if (c == '"') {
      quoted = true;
    } else if (c == '.') {
      parts.push_back(part);
      part.clear();
    } else {
      part += c;
    }
  }
  parts.push_back(part);
  return parts;
}

std::string ObjectPath::ToString() const {
  if (IsTableFunction()) return QuoteIdentifierIfNeeded(object_name);
  std::string out = QuoteIdentifierIfNeeded(catalog);
  for (const auto& part : schema_path) out += "." + QuoteIdentifierIfNeeded(part);
  if (!object_name.empty()) out += "." + QuoteIdentifierIfNeeded(object_name);
  return out;
}

std::string SystemPrivilegeName(SystemPrivilege privilege) {
  switch (privilege) {
    case SystemPrivilege::kManageCatalogs:
      return "MANAGE CATALOGS";
    case SystemPrivilege::kManageExtensions:
      return "MANAGE EXTENSIONS";
    case SystemPrivilege::kManageSecrets:
      return "MANAGE SECRETS";
    case SystemPrivilege::kManageConfig:
      return "MANAGE CONFIG";
    case SystemPrivilege::kReadFilesystem:
      return "READ FILESYSTEM";
    case SystemPrivilege::kWriteFilesystem:
      return "WRITE FILESYSTEM";
    case SystemPrivilege::kExportDatabase:
      return "EXPORT DATABASE";
    case SystemPrivilege::kImportDatabase:
      return "IMPORT DATABASE";
    case SystemPrivilege::kCheckpoint:
      return "CHECKPOINT";
    case SystemPrivilege::kReadObjectStorage:
      return "READ OBJECT STORAGE";
    case SystemPrivilege::kWriteObjectStorage:
      return "WRITE OBJECT STORAGE";
    case SystemPrivilege::kReadHttp:
      return "READ HTTP";
    case SystemPrivilege::kWriteHttp:
      return "WRITE HTTP";
  }
  return "UNKNOWN";
}

const std::vector<SystemPrivilege>& AllSystemPrivileges() {
  static const std::vector<SystemPrivilege> kAll = {
      SystemPrivilege::kManageCatalogs, SystemPrivilege::kManageExtensions,
      SystemPrivilege::kManageSecrets,  SystemPrivilege::kManageConfig,
      SystemPrivilege::kReadFilesystem, SystemPrivilege::kWriteFilesystem,
      SystemPrivilege::kExportDatabase, SystemPrivilege::kImportDatabase,
      SystemPrivilege::kCheckpoint,     SystemPrivilege::kReadObjectStorage,
      SystemPrivilege::kWriteObjectStorage, SystemPrivilege::kReadHttp,
      SystemPrivilege::kWriteHttp};
  return kAll;
}

std::optional<SystemPrivilege> SystemPrivilegeFromName(const std::string& name) {
  for (auto p : AllSystemPrivileges()) {
    if (SystemPrivilegeName(p) == name) return p;
  }
  return std::nullopt;
}

std::string HiddenObjectMessage(const std::string& user_name) {
  return "Hardcharger blocked this statement: it names a catalog, schema, table or view "
         "that does not "
         "exist, or that user " +
         user_name + " cannot see";
}

bool IsSensitiveSetting(const std::string& name) {
  std::string lower = name;
  for (auto& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  for (const char* word : {"password", "secret", "token", "access_key", "key_id", "account_key",
                           "connection_string", "credential", "private_key"}) {
    if (lower.find(word) != std::string::npos) return true;
  }
  return false;
}

}  // namespace hardcharger::authz
