// Ported from GizmoSQL 1.x src/client/shell_completer.hpp.

#pragma once

#include <string>
#include <vector>

#include <replxx.hxx>

#include "warp_connection.hpp"

namespace hardcharger::client {

struct TableEntry {
  std::string catalog;
  std::string schema;
  std::string table_name;
  std::string table_type;
};

struct SchemaEntry {
  std::string catalog;
  std::string schema_name;
};

class ShellCompleter {
 public:
  explicit ShellCompleter(WarpConnection& conn);

  // Completion callback for replxx
  replxx::Replxx::completions_t Complete(const std::string& input,
                                         int& context_len);

  // Hint callback for replxx
  replxx::Replxx::hints_t Hint(const std::string& input, int& context_len,
                                replxx::Replxx::Color& color);

  // Invalidate the schema cache (call after DDL or .connect)
  void InvalidateCache();

  // Get the list of SQL keywords (for testing)
  static const std::vector<std::string>& GetSqlKeywords();

  // Get the list of dot commands (for testing)
  static const std::vector<std::string>& GetDotCommands();

 private:
  enum class CompletionContext {
    DOT_COMMAND,
    SCHEMA_QUALIFIED,
    TABLE_NAME,
    SQL_GENERAL,
  };

  void EnsureCachePopulated();
  CompletionContext DetermineContext(const std::string& input,
                                    const std::string& prefix) const;
  std::string ExtractPrefix(const std::string& input, int& context_len) const;

  // Apply case matching: if user typed lowercase prefix, return lowercase
  static std::string MatchCase(const std::string& candidate,
                                const std::string& prefix);

  WarpConnection& conn_;
  std::vector<TableEntry> tables_;
  std::vector<SchemaEntry> schemas_;
  bool cache_populated_ = false;
};

}  // namespace hardcharger::client
