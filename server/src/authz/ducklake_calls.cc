#include "authz/ducklake_calls.hpp"

#include <algorithm>
#include <cctype>
#include <duckdb/parser/parser.hpp>
#include <duckdb/parser/simplified_token.hpp>
#include <unordered_map>
#include <unordered_set>

namespace hardcharger::authz {

namespace {

namespace dd = duckdb;

std::string Lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
  return s;
}

// Every ducklake_* table function DuckDB 2.0's DuckLake has (2026-11-20: SELECT function_name
// FROM duckdb_functions() WHERE function_name LIKE 'ducklake%'), GizmoSQL 1.42.3's names, and
// the branch functions of customer DuckLake builds. Read: GizmoSQL's DuckLakeReadMethods.
// Write: its DuckLakeWriteMethods, the commit message (it rewrites the next snapshot's
// record), and every branch function but the three that only look.
const std::unordered_map<std::string, DuckLakeAccess>& KnownFunctions() {
  static const std::unordered_map<std::string, DuckLakeAccess> kFunctions = {
      {"ducklake_snapshots", DuckLakeAccess::kRead},
      {"ducklake_table_info", DuckLakeAccess::kRead},
      {"ducklake_list_files", DuckLakeAccess::kRead},
      {"ducklake_table_insertions", DuckLakeAccess::kRead},
      {"ducklake_table_deletions", DuckLakeAccess::kRead},
      {"ducklake_table_changes", DuckLakeAccess::kRead},
      {"ducklake_settings", DuckLakeAccess::kRead},
      {"ducklake_options", DuckLakeAccess::kRead},
      {"ducklake_get_option", DuckLakeAccess::kRead},
      {"ducklake_current_snapshot", DuckLakeAccess::kRead},
      {"ducklake_last_committed_snapshot", DuckLakeAccess::kRead},
      {"ducklake_branches", DuckLakeAccess::kRead},
      {"ducklake_current_branch", DuckLakeAccess::kRead},
      {"ducklake_branch_table", DuckLakeAccess::kRead},
      {"ducklake_merge_adjacent_files", DuckLakeAccess::kWrite},
      {"ducklake_expire_snapshots", DuckLakeAccess::kWrite},
      {"ducklake_cleanup_old_files", DuckLakeAccess::kWrite},
      {"ducklake_delete_orphaned_files", DuckLakeAccess::kWrite},
      {"ducklake_flush_inlined_data", DuckLakeAccess::kWrite},
      {"ducklake_rewrite_data_files", DuckLakeAccess::kWrite},
      {"ducklake_set_option", DuckLakeAccess::kWrite},
      {"ducklake_set_commit_message", DuckLakeAccess::kWrite},
      // Registers files by path in a catalog: a token could pull another catalog's
      // data files into its own.
      {"ducklake_add_data_files", DuckLakeAccess::kAdministrator},
      // DuckLake's own: a Parquet reader over a path, and a commit addressed by
      // metadata schema. Neither's first argument is a catalog.
      {"ducklake_scan", DuckLakeAccess::kAdministrator},
      {"ducklake_commit", DuckLakeAccess::kAdministrator},
  };
  return kFunctions;
}

bool IsWordToken(const dd::SimplifiedToken& token) {
  return token.type == dd::SimplifiedTokenType::SIMPLIFIED_TOKEN_IDENTIFIER ||
         token.type == dd::SimplifiedTokenType::SIMPLIFIED_TOKEN_KEYWORD;
}

// The token's text: up to the next token, without trailing blanks; a quoted identifier
// without its quotes.
std::string TokenText(const std::string& sql, const std::vector<dd::SimplifiedToken>& tokens, size_t i) {
  const size_t start = std::min<size_t>(tokens[i].start, sql.size());
  const size_t end = i + 1 < tokens.size() ? std::min<size_t>(tokens[i + 1].start, sql.size()) : sql.size();
  std::string text = sql.substr(start, end > start ? end - start : 0);
  while (!text.empty() && std::isspace(static_cast<unsigned char>(text.back()))) text.pop_back();
  if (text.size() >= 2 && text.front() == '"' && text.back() == '"') {
    std::string unquoted;
    for (size_t k = 1; k + 1 < text.size(); ++k) {
      unquoted.push_back(text[k]);
      if (text[k] == '"' && k + 2 < text.size() && text[k + 1] == '"') ++k;
    }
    return unquoted;
  }
  return text;
}

}  // namespace

std::optional<DuckLakeAccess> DuckLakeFunctionAccess(const std::string& name_lower) {
  if (name_lower.rfind("ducklake_", 0) != 0) return std::nullopt;
  const auto& known = KnownFunctions();
  if (auto it = known.find(name_lower); it != known.end()) return it->second;
  // Any other branch function of a customer build changes a branch.
  if (name_lower.find("branch") != std::string::npos) return DuckLakeAccess::kWrite;
  return DuckLakeAccess::kUnknown;
}

bool IsDuckLakeCatalogMethod(const std::string& name_lower) {
  // DuckDB 2.0's DuckLake catalog macros, and GizmoSQL 1.42.3's names for the rest.
  static const std::unordered_set<std::string> kMethods = {
      "snapshots",        "table_info",          "options",          "settings",
      "current_snapshot", "last_committed_snapshot", "merge_adjacent_files", "set_option",
      "set_commit_message", "table_changes",     "table_insertions", "table_deletions",
      "list_files",       "get_option",          "expire_snapshots", "cleanup_old_files",
      "delete_orphaned_files", "flush_inlined_data", "rewrite_data_files", "add_data_files",
      "branches",         "current_branch",      "branch_table"};
  return kMethods.contains(name_lower);
}

std::vector<std::string> DuckLakeBoundFunctions(const std::string& function_lower) {
  if (function_lower == "ducklake_table_changes") {
    return {"ducklake_table_insertions", "ducklake_table_deletions"};
  }
  return {function_lower};
}

Privilege DuckLakeCatalogPrivilege(DuckLakeAccess access) {
  return access == DuckLakeAccess::kRead ? Privilege::kSelect : Privilege::kMaintain;
}

std::string DuckLakeExecuteRefusal(const std::string& function) {
  // As the gate words any table function without a grant, naming the function that runs.
  return "Hardcharger blocked the table function " + function +
         ": under access control it is available to the administrator only, or to a role granted EXECUTE ON "
         "TABLE FUNCTION " + function;
}

std::string DuckLakeCatalogPrivilegeRefusal(const std::string& function, const std::string& catalog,
                                            DuckLakeAccess access, const std::string& user) {
  const std::string privilege = PrivilegeName(DuckLakeCatalogPrivilege(access));
  return "Hardcharger blocked " + function + "() on catalog " + catalog + ": it " +
         (access == DuckLakeAccess::kRead ? "reads" : "maintains") + " the whole catalog, which needs " + privilege +
         " ON CATALOG " + catalog + ", and user " + user +
         " does not hold it (EXECUTE on the function does not reach a catalog by itself)";
}

std::string DuckLakeAdministratorRefusal(const std::string& function) {
  return "Hardcharger blocked " + function +
         "(): it reaches files or metadata by path, not through a catalog's access rules, so only an "
         "administrator without catalog_access rules may call it";
}

std::string DuckLakeUnknownRefusal(const std::string& function) {
  return "Hardcharger blocked " + function +
         "(): access control does not know which catalog this DuckLake function reaches or whether it "
         "writes, so only an administrator without catalog_access rules may call it";
}

std::string DuckLakeWrappedRefusal(const std::string& function, const std::string& wrapper) {
  return "Hardcharger blocked " + function + "() in " + wrapper +
         ": a view or macro cannot wrap a DuckLake function (it would run with the access of whoever "
         "queries it); call it directly";
}

std::string DuckLakeComputedCatalogRefusal(const std::string& function) {
  return "Hardcharger blocked " + function +
         "(): its catalog must be a string literal (ducklake_snapshots('my_lake')), so access control can "
         "check it before the function runs";
}

std::string DuckLakeUncheckedRefusal(const std::string& function) {
  return "Hardcharger blocked " + function +
         "(): it is called where access control cannot check its catalog before it runs (inside a macro, "
         "or in a statement form the check does not read); call it directly, in FROM or with CALL";
}

int CountDuckLakeCallTokens(const std::string& sql, std::string* first) {
  std::vector<dd::SimplifiedToken> all;
  try {
    all = dd::Parser::Tokenize(sql);
  } catch (const std::exception&) {
    return 0;  // the parser reports what it makes of the text
  }
  // Tokens without comments, keeping their text.
  std::vector<std::string> text;
  std::vector<bool> word;
  for (size_t i = 0; i < all.size(); ++i) {
    if (all[i].type == dd::SimplifiedTokenType::SIMPLIFIED_TOKEN_COMMENT) continue;
    text.push_back(TokenText(sql, all, i));
    word.push_back(IsWordToken(all[i]));
  }
  int count = 0;
  for (size_t i = 0; i < text.size(); ++i) {
    if (!word[i] || i + 1 >= text.size() || text[i + 1].empty() || text[i + 1][0] != '(') continue;
    const std::string name = Lower(text[i]);
    const bool prefixed = name.rfind("ducklake_", 0) == 0;
    const bool method = i > 0 && text[i - 1] == "." && IsDuckLakeCatalogMethod(name);
    if (!prefixed && !method) continue;
    if (first && first->empty()) *first = prefixed ? name : "ducklake_" + name;
    ++count;
  }
  return count;
}

bool LooksLikeDuckLakeCall(const std::vector<std::string>& parts) {
  if (parts.empty()) return false;
  const std::string name = Lower(parts.back());
  return name.rfind("ducklake_", 0) == 0 || (parts.size() >= 2 && IsDuckLakeCatalogMethod(name));
}

}  // namespace hardcharger::authz
