#include "parse_guard.hpp"

#include <duckdb.hpp>
#include <duckdb/common/exception.hpp>
#include <duckdb/common/string_util.hpp>
#include <duckdb/catalog/catalog.hpp>
#include <duckdb/catalog/catalog_entry/scalar_function_catalog_entry.hpp>
#include <duckdb/catalog/catalog_entry/table_function_catalog_entry.hpp>
#include <duckdb/catalog/catalog_transaction.hpp>
#include <duckdb/common/file_system.hpp>
#include <duckdb/function/pragma_function.hpp>
#include <duckdb/main/capi_v2/capi_v2_internal.hpp>
#include <duckdb/main/client_context.hpp>
#include <duckdb/main/config.hpp>
#include <duckdb/main/extension_helper.hpp>
#include <duckdb/main/extension_manager.hpp>
#include <duckdb/parser/parser.hpp>
#include <duckdb/parser/parsed_data/create_pragma_function_info.hpp>
#include <duckdb/parser/parser_extension.hpp>
#include <duckdb/parser/statement/copy_statement.hpp>
#include <duckdb/parser/peg/keyword_helper/duckdb_keyword_helper.hpp>
#include <duckdb/parser/peg/tokenizer/tokenizer.hpp>
#include <duckdb/planner/operator/logical_reset.hpp>
#include <duckdb/planner/operator/logical_set.hpp>
#include <duckdb/planner/extension_callback.hpp>
#include <duckdb/planner/planner_extension.hpp>

#include <algorithm>
#include <atomic>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace hardcharger {

namespace dd = duckdb;

namespace {

bool IsWordToken(dd::TokenType type, std::string_view text) {
  switch (type) {
    case dd::TokenType::STRING_LITERAL:
    case dd::TokenType::NUMBER_LITERAL:
    case dd::TokenType::OPERATOR:
    case dd::TokenType::COMMENT:
    case dd::TokenType::TERMINATOR:
    case dd::TokenType::TOKEN_ERROR:
    case dd::TokenType::END_OF_INPUT:
    case dd::TokenType::END_OF_INPUT_AUTOCOMPLETE:
    case dd::TokenType::INVALID:
      return false;
    default:  // a keyword or a name, quoted or not
      return !text.empty();
  }
}

bool SameWord(std::string_view text, std::string_view lower) {
  return text.size() == lower.size() && std::equal(text.begin(), text.end(), lower.begin(), [](char a, char b) {
           return dd::StringUtil::CharacterToLower(a) == b;
         });
}

// Reads DuckDB's tokens as its tokenizer makes them and keeps only counts. The tokenizer
// hands some tokens to PushToken and appends the others (single-character operators among
// them) to the vector it was given; both arrive here in the order of the text, and the
// vector is emptied as it is read, so a text's tokens are never all held at once.
class ShapeReader final : public dd::TokenizerBehavior {
 public:
  ShapeReader(const dd::string& sql, dd::vector<dd::MatcherToken>& pending) : dd::TokenizerBehavior(sql, pending) {}

  void PushToken(dd::idx_t start, dd::idx_t end, dd::TokenType type, bool unterminated) override {
    Drain();
    if (unterminated && type == dd::TokenType::COMMENT) shape_.refused_by_tokenizer = true;
    if (start < end && end <= sql.size()) Read(type, std::string_view(sql).substr(start, end - start), start);
  }
  void OnStatementEnd(dd::idx_t) override {
    Drain();
    StatementEnds();
  }
  void OnLastToken(const dd::Tokenizer&, dd::TokenizeState state, dd::string last_word, dd::idx_t last_pos) override {
    Drain();
    if (state == dd::TokenizeState::STRING_LITERAL || state == dd::TokenizeState::QUOTED_IDENTIFIER ||
        state == dd::TokenizeState::MULTI_LINE_COMMENT) {
      shape_.refused_by_tokenizer = true;
    }
    if (!last_word.empty()) Read(dd::Tokenizer::TokenizeStateToType(state), last_word, last_pos);
  }

  SqlShape Finish() {
    Drain();
    StatementEnds();
    shape_.nesting = nesting_;
    return shape_;
  }

 private:
  void Drain() {
    for (const dd::MatcherToken& token : tokens) Read(token.type, token.text, token.offset);
    tokens.clear();
  }

  void StatementEnds() {
    if (open_ > shape_.unclosed) {
      shape_.unclosed = open_;
      shape_.position = first_;
    }
    open_ = brackets_ = 0;
    from_.assign(1, 0);
    parts_ = 0;
    after_dot_ = false;
  }

  void Counted(std::size_t tables) { shape_.tables = std::max(shape_.tables, tables); }

  void Read(dd::TokenType type, std::string_view text, std::size_t offset) {
    if (type == dd::TokenType::COMMENT) return;  // (nothing to DuckDB's parser)
    if (type == dd::TokenType::END_OF_INPUT || type == dd::TokenType::END_OF_INPUT_AUTOCOMPLETE) return;
    const bool word = IsWordToken(type, text);
    // A dotted name: a.b.c. (A lone "." is a number token; 1.5 is one token.)
    if (word) {
      parts_ = after_dot_ ? parts_ + 1 : 1;
      after_dot_ = false;
      shape_.name_parts = std::max(shape_.name_parts, parts_);
    } else if (type == dd::TokenType::NUMBER_LITERAL && text == "." && parts_ > 0 && !after_dot_) {
      after_dot_ = true;
    } else {
      parts_ = 0;
      after_dot_ = false;
    }
    if (type == dd::TokenType::TERMINATOR) {
      StatementEnds();
      return;
    }
    if (word) {
      // FROM a, b JOIN c ...: one more table for each comma and each JOIN, until the clause
      // ends. (x IS DISTINCT FROM y is no clause, nor extract(year FROM d)'s FROM... which
      // opens no list of tables anyway: a comma there is inside the call's own brackets.)
      if (SameWord(text, "from")) {
        if (!previous_was_distinct_) Counted(from_.back() = 1);
      } else if (SameWord(text, "join")) {
        if (from_.back() > 0) Counted(++from_.back());
      } else if (from_.back() > 0) {
        static constexpr std::string_view kEndsFrom[] = {
            "where",  "group",   "order",     "having", "limit", "offset", "union",  "except",
            "intersect", "window", "qualify", "returning", "select", "set",   "values", "sample"};
        for (const std::string_view end : kEndsFrom) {
          if (SameWord(text, end)) from_.back() = 0;
        }
      }
      previous_was_distinct_ = SameWord(text, "distinct");
      return;
    }
    if (type != dd::TokenType::OPERATOR || text.size() != 1) return;
    switch (text[0]) {
      case ',':
        if (from_.back() > 0) Counted(++from_.back());
        break;
      case '(':
        if (open_++ == 0) first_ = offset;
        [[fallthrough]];
      case '[':
      case '{':
        nesting_ = std::max(nesting_, ++brackets_);
        from_.push_back(0);
        break;
      case ')':
        if (open_ > 0) open_--;  // (one too many is DuckDB's to refuse: it does so at once)
        [[fallthrough]];
      case ']':
      case '}':
        if (brackets_ > 0) brackets_--;
        if (from_.size() > 1) from_.pop_back();
        break;
      default:
        break;
    }
  }

  SqlShape shape_;
  std::size_t open_ = 0;       // parentheses open in this statement
  std::size_t first_ = 0;      // where the outermost of them is
  std::size_t brackets_ = 0;   // ( [ { open in this statement
  std::size_t nesting_ = 0;    // the deepest they got anywhere
  // For each bracket open (and the statement itself): the tables its FROM clause has named
  // so far, or 0 outside a FROM clause.
  std::vector<std::size_t> from_ = std::vector<std::size_t>(1, 0);
  bool previous_was_distinct_ = false;
  std::size_t parts_ = 0;      // of the dotted name being read
  bool after_dot_ = false;
};

SqlShape Measure(const std::string& sql, const dd::Tokenizer& tokenizer) {
  dd::vector<dd::MatcherToken> pending;
  ShapeReader reader(sql, pending);
  try {
    tokenizer.TokenizeInput(reader);
  } catch (const std::exception&) {
    // The same tokenizer refuses the text at the same place when DuckDB's parser runs it:
    // what was read so far is all there is to judge.
  }
  return reader.Finish();
}

const dd::Tokenizer& DefaultTokenizer() {
  static const dd::Tokenizer tokenizer(dd::DuckDBKeywordHelper::Instance());
  return tokenizer;
}

}  // namespace

SqlShape MeasureSql(const std::string& sql) { return Measure(sql, DefaultTokenizer()); }

std::string ShapeRefusal(const SqlShape& shape, std::size_t nesting_limit) {
  if (shape.refused_by_tokenizer) return {};  // DuckDB's error is the right one, and is at once
  if (shape.unclosed > 0) {
    return "syntax error: " + std::to_string(shape.unclosed) +
           (shape.unclosed == 1 ? " opening parenthesis is" : " opening parentheses are") +
           " never closed (the first \"(\" at position " + std::to_string(shape.position + 1) + ")";
  }
  if (nesting_limit == 0) return {};
  if (shape.nesting > nesting_limit) {
    return "the statement's brackets nest " + std::to_string(shape.nesting) + " deep, and this server takes " +
           std::to_string(nesting_limit) + " (its max_sql_nesting option)";
  }
  if (shape.tables > 4 * nesting_limit) {
    return "a FROM clause of the statement names " + std::to_string(shape.tables) + " tables, and this server takes " +
           std::to_string(4 * nesting_limit) + " in one (four times its max_sql_nesting option)";
  }
  if (shape.name_parts > nesting_limit) {
    return "a name in the statement has " + std::to_string(shape.name_parts) +
           " dotted parts, and this server takes " + std::to_string(nesting_limit) + " (its max_sql_nesting option)";
  }
  return {};
}

// ---- The hook and the setting guard ----

struct ParseGuard::State : dd::ParserExtensionInfo, dd::PlannerExtensionInfo {
  State(std::size_t limit, AutoCompletePolicy policy) : nesting_limit(limit), autocomplete(std::move(policy)) {}
  const std::size_t nesting_limit;
  const AutoCompletePolicy autocomplete;
  std::atomic<bool> locked = false;
};

namespace {

// DuckDB calls this with every text a connection's parser is asked to parse, already
// normalized (NormalizeSQLString), before its own parser runs. A refusal is thrown: under
// 'fallback' an override's error result would be ignored, and under 'strict' a later
// extension's could clear it; a throw ends the parse whatever else is registered.
dd::ParserOverrideResult GuardParse(dd::ParserExtensionInfo* info, const dd::string& query, dd::ParserOptions&) {
  const auto& state = static_cast<const ParseGuard::State&>(*info);
  // DuckDB's tokenizer class, the one its grammar's parser runs (a grammar extension changes
  // only which words are keywords, which the counts here do not depend on; sql_lex.hpp).
  const std::string refusal = ShapeRefusal(Measure(query, DefaultTokenizer()), state.nesting_limit);
  if (!refusal.empty()) throw dd::ParserException(refusal);
  return dd::ParserOverrideResult();  // not claimed: DuckDB's own parser parses it
}

void Refuse(const dd::Identifier& name, const ParseGuard::State& state, const dd::Value* value, dd::SetScope scope) {
  const auto option = dd::DBConfig::GetOptionByName(name);
  if (!option) return;
  const std::string_view canonical = option->name;
  if (canonical == kParserOverrideSetting) {
    throw dd::InvalidInputException(
        "%s cannot be changed on this server: the server parses every statement through its own check first (the "
        "statement shapes DuckDB's parser cannot be handed safely, README \"Statements refused before DuckDB parses "
        "them\"), and this setting would switch that check off",
        kParserOverrideSetting);
  }
  if (canonical != "max_expression_depth") return;
  const std::size_t limit = state.nesting_limit;
  bool deeper = false;
  if (value == nullptr) {
    // RESET: a session's falls back to the instance's value (the limit); the instance's own
    // to DuckDB's default of 1000.
    deeper = (scope == dd::SetScope::GLOBAL ||
              (scope == dd::SetScope::AUTOMATIC && option->scope != dd::SettingScopeTarget::LOCAL_ONLY &&
               option->scope != dd::SettingScopeTarget::LOCAL_DEFAULT)) &&
             limit < 1000;
  } else {
    const auto depth = value->IsNull() ? dd::optional<dd::Value>() : value->DefaultTryCastAs(dd::LogicalType::UBIGINT);
    deeper = !depth || depth->IsNull() || depth->GetValue<uint64_t>() > limit;
  }
  if (deeper) {
    throw dd::InvalidInputException(
        "max_expression_depth can be set to a number up to %llu on this server: deeper nesting would overflow the "
        "stack of the threads that run statements. The server's max_sql_nesting option raises the limit.",
        static_cast<unsigned long long>(limit));
  }
}

// After binding, before anything runs: SET and RESET anywhere in the plan (EXPLAIN ANALYZE
// runs what it explains).
void GuardSettings(dd::PlannerExtensionInput& input, dd::BoundStatement& statement) {
  if (!input.info || !statement.plan) return;
  const auto& state = static_cast<const ParseGuard::State&>(*input.info);
  if (!state.locked.load()) return;
  std::vector<dd::LogicalOperator*> pending{statement.plan.get()};
  while (!pending.empty()) {
    dd::LogicalOperator* op = pending.back();
    pending.pop_back();
    if (op->type == dd::LogicalOperatorType::LOGICAL_SET) {
      auto& set = op->Cast<dd::LogicalSet>();
      if (set.scope != dd::SetScope::VARIABLE) Refuse(set.name, state, &set.value, set.scope);
    } else if (op->type == dd::LogicalOperatorType::LOGICAL_RESET) {
      auto& reset = op->Cast<dd::LogicalReset>();
      if (reset.scope != dd::SetScope::VARIABLE) Refuse(reset.name, state, nullptr, reset.scope);
    }
    for (auto& child : op->children) pending.push_back(child.get());
  }
}

// IMPORT DATABASE 'dir' (PRAGMA import_database): DuckDB's own reads dir/load.sql with its
// built-in parser, which no parser hook sees (2,000 unclosed parentheses in the file: 1 s;
// 4,000: 4 s; quadratic). This one is DuckDB's, word for word, except that load.sql is
// parsed by the connection's parser, through the guard, and a statement in it that is no
// COPY is an error (which DuckDB checks only in debug builds).
dd::string GuardedImportDatabase(dd::ClientContext& context, const dd::FunctionParameters& parameters) {
  auto& fs = dd::FileSystem::GetFileSystem(context);
  dd::string final_query;
  for (const char* file : {"schema.sql", "load.sql"}) {
    const auto file_path = fs.JoinPath(parameters.values[0].ToString(), file);
    auto handle = fs.OpenFile(file_path, dd::FileFlags::FILE_FLAGS_READ);
    const auto fsize = fs.GetFileSize(*handle);
    auto buffer = dd::make_unsafe_uniq_array<char>(dd::UnsafeNumericCast<size_t>(fsize));
    fs.Read(*handle, buffer.get(), fsize);
    auto query = dd::string(buffer.get(), dd::UnsafeNumericCast<uint32_t>(fsize));
    if (std::string_view(file) == "load.sql") {
      // Replace the placeholder with the path provided to IMPORT.
      dd::Parser parser(context);
      parser.ParseQuery(query);
      auto copy_statements = std::move(parser.statements);
      query.clear();
      for (auto& statement_p : copy_statements) {
        if (statement_p->type != dd::StatementType::COPY_STATEMENT) {
          throw dd::InvalidInputException("IMPORT DATABASE: %s holds a statement that is not a COPY", file_path);
        }
        auto& statement = statement_p->Cast<dd::CopyStatement>();
        auto& info = *statement.info;
        auto file_name = fs.ExtractName(info.file_path);
        info.file_path = fs.JoinPath(parameters.values[0].ToString(), file_name);
        query += statement.ToString() + ";";
      }
    }
    final_query += query;
  }
  return final_query;
}

// ---- The autocomplete extension's functions ----

// The guard the wrapped functions answer to: one server per process (embedding.cc says so).
std::atomic<const ParseGuard::State*> autocomplete_state = nullptr;

// `growth`: how the function's time grows with its input.
void CheckAutoComplete(const char* function, const char* growth) {
  const ParseGuard::State* state = autocomplete_state.load();
  if (state == nullptr) return;  // (a guard that never locked: startup, the operator's init SQL)
  if (!state->autocomplete.enabled) {
    throw dd::PermissionException(
        "%s() is turned off on this server: %s, and neither a statement timeout nor a cancel interrupts it. The "
        "server's enable_sql_auto_complete option turns it on, for administrators.",
        function, growth);
  }
  if (!state->autocomplete.is_administrator || !state->autocomplete.is_administrator()) {
    throw dd::PermissionException(
        "%s() is for administrators on this server: %s, and neither a statement timeout nor a cancel interrupts it",
        function, growth);
  }
}

constexpr char kMatcherGrowth[] =
    "it runs DuckDB's SQL matcher on its argument, whose time grows exponentially with the brackets the argument "
    "leaves open";
constexpr char kFormatterGrowth[] =
    "its time and its result grow with the square of its argument (4,000 nested subqueries: 3 s and 64 MB)";

// The bind callbacks the extension registered: the wrappers call them once the check passes.
std::atomic<dd::table_function_bind_t> original_auto_complete = nullptr;
std::atomic<dd::table_function_bind_t> original_tokenize = nullptr;
std::atomic<dd::table_function_bind_t> original_check_peg_parser = nullptr;
std::atomic<dd::bind_scalar_function_t> original_format_sql = nullptr;

dd::unique_ptr<dd::FunctionData> GuardedAutoComplete(dd::ClientContext& context, dd::TableFunctionBindInput& input,
                                                     dd::vector<dd::LogicalType>& types, dd::vector<dd::Identifier>& names) {
  CheckAutoComplete("sql_auto_complete", kMatcherGrowth);
  return original_auto_complete.load()(context, input, types, names);
}
dd::unique_ptr<dd::FunctionData> GuardedTokenize(dd::ClientContext& context, dd::TableFunctionBindInput& input,
                                                 dd::vector<dd::LogicalType>& types, dd::vector<dd::Identifier>& names) {
  CheckAutoComplete("sql_tokenize", kMatcherGrowth);
  return original_tokenize.load()(context, input, types, names);
}
dd::unique_ptr<dd::FunctionData> GuardedCheckPegParser(dd::ClientContext& context, dd::TableFunctionBindInput& input,
                                                       dd::vector<dd::LogicalType>& types,
                                                       dd::vector<dd::Identifier>& names) {
  CheckAutoComplete("check_peg_parser", kMatcherGrowth);
  return original_check_peg_parser.load()(context, input, types, names);
}
dd::unique_ptr<dd::FunctionData> GuardedFormatSql(dd::BindScalarFunctionInput& input) {
  // (The formatting itself runs when the statement executes, on any thread: binding is the
  // statement's own thread, where the session is known.)
  CheckAutoComplete("duckdb_format_sql", kFormatterGrowth);
  auto original = original_format_sql.load();
  return original != nullptr ? original(input) : nullptr;
}

// Replaces each overload of the named table function with a copy bound by `guarded`.
void WrapTableFunction(dd::ClientContext& context, const char* name, std::atomic<dd::table_function_bind_t>& original,
                       dd::table_function_bind_t guarded) {
  auto entry = dd::Catalog::GetEntry<dd::TableFunctionCatalogEntry>(
      context, dd::QualifiedName(dd::Identifier(SYSTEM_CATALOG), dd::Identifier::DefaultSchema(), dd::Identifier(name)),
      dd::OnEntryNotFound::RETURN_NULL);
  if (!entry) {
    throw std::runtime_error(std::string("the autocomplete extension is loaded, but its ") + name +
                             "() is not where the server looks for it: the server refuses to start rather than leave it "
                             "unguarded");
  }
  for (auto& function : entry->functions.functions) {
    if (function->bind == guarded) continue;
    if (function->bind == nullptr) throw std::runtime_error(std::string(name) + "() has no bind callback to guard");
    original.store(function->bind);
    dd::TableFunction copy = *function;
    copy.bind = guarded;
    function = dd::make_shared_ptr<const dd::TableFunction>(std::move(copy));
  }
}

void WrapAutoComplete(dd::ClientContext& context) {
  context.RunFunctionInTransaction([&] {
    WrapTableFunction(context, "sql_auto_complete", original_auto_complete, GuardedAutoComplete);
    WrapTableFunction(context, "sql_tokenize", original_tokenize, GuardedTokenize);
    WrapTableFunction(context, "check_peg_parser", original_check_peg_parser, GuardedCheckPegParser);
    auto entry = dd::Catalog::GetEntry<dd::ScalarFunctionCatalogEntry>(
        context,
        dd::QualifiedName(dd::Identifier(SYSTEM_CATALOG), dd::Identifier::DefaultSchema(),
                          dd::Identifier("duckdb_format_sql")),
        dd::OnEntryNotFound::RETURN_NULL);
    if (!entry) {
      throw std::runtime_error("the autocomplete extension is loaded, but its duckdb_format_sql() is not where the "
                               "server looks for it: the server refuses to start rather than leave it unguarded");
    }
    for (auto& function : entry->functions.functions) {
      if (function->GetBindCallback() == GuardedFormatSql) continue;
      // (Every overload binds with the same callback, or none: the check needs one either way.)
      if (function->GetBindCallback() != nullptr) original_format_sql.store(function->GetBindCallback());
      dd::ScalarFunction copy = *function;
      copy.SetBindCallback(GuardedFormatSql);
      function = dd::make_shared_ptr<const dd::ScalarFunction>(std::move(copy));
    }
  });
}

// Loading the extension: during startup only, and only when the option is on (a statically
// linked one is loaded before any of this and wrapped at Lock()).
class AutoCompleteLoads final : public dd::ExtensionCallback {
 public:
  explicit AutoCompleteLoads(const ParseGuard::State& state) : state_(state) {}
  void OnBeginExtensionLoad(dd::DatabaseInstance&, const dd::string& name) override {
    if (!dd::StringUtil::CIEquals(name, kAutoCompleteExtension)) return;
    if (!state_.autocomplete.enabled) {
      throw dd::PermissionException(
          "the autocomplete extension is turned off on this server: its functions run DuckDB's SQL matcher, whose time "
          "grows exponentially with the brackets their input leaves open, uninterrupted. The server's "
          "enable_sql_auto_complete option turns it on (for administrators), loaded at startup.");
    }
    if (state_.locked.load()) {
      throw dd::PermissionException(
          "the autocomplete extension is loaded at startup only on this server (enable_sql_auto_complete), so that "
          "its functions are guarded before any session runs; it could not be loaded then");
    }
  }

 private:
  const ParseGuard::State& state_;
};

}  // namespace

std::shared_ptr<ParseGuard> ParseGuard::Install(duckdb_v2_instance_handle instance, std::size_t nesting_limit,
                                               AutoCompletePolicy autocomplete) {
  auto& database = *dd::capiv2::Convert(instance)->GetDatabase().instance;
  auto& config = dd::DBConfig::GetConfig(database);
  auto state = dd::make_shared_ptr<State>(nesting_limit, std::move(autocomplete));
  dd::ParserExtension parser;
  parser.parser_override = GuardParse;
  parser.parser_info = dd::shared_ptr<dd::ParserExtensionInfo>(state, static_cast<dd::ParserExtensionInfo*>(state.get()));
  dd::ParserExtension::Register(config, std::move(parser));
  dd::PlannerExtension planner;
  planner.post_bind_function = GuardSettings;
  planner.planner_info =
      dd::shared_ptr<dd::PlannerExtensionInfo>(state, static_cast<dd::PlannerExtensionInfo*>(state.get()));
  dd::PlannerExtension::Register(config, std::move(planner));
  dd::ExtensionCallback::Register(config, dd::make_shared_ptr<AutoCompleteLoads>(*state));
  {
    // The one parse of SQL text that is not a connection's: IMPORT DATABASE's of load.sql.
    dd::CreatePragmaFunctionInfo info(dd::PragmaFunction::PragmaCall(
        "import_database", GuardedImportDatabase, dd::FunctionSignature().AddPositionalOnly("path", dd::LogicalType::VARCHAR)));
    info.on_conflict = dd::OnCreateConflict::REPLACE_ON_CONFLICT;
    auto transaction = dd::CatalogTransaction::GetSystemTransaction(database);
    dd::Catalog::GetSystemCatalog(database).CreatePragmaFunction(transaction, info);
  }
  // DuckDB's config holds the State for as long as the instance lives; the guard holds it
  // too (a std::shared_ptr that owns a copy of DuckDB's shared pointer).
  struct Holder {
    dd::shared_ptr<State> state;
  };
  auto holder = std::make_shared<Holder>(Holder{state});
  return std::shared_ptr<ParseGuard>(
      new ParseGuard(std::shared_ptr<State>(holder, holder->state.get()), nesting_limit));
}

void ParseGuard::Lock(duckdb_v2_connection_handle connection) {
  auto& context = *dd::capiv2::Convert(connection)->context;
  autocomplete_state.store(state_.get());
  auto& extensions = dd::ExtensionManager::Get(context);
  if (state_->autocomplete.enabled && !extensions.ExtensionIsLoaded(kAutoCompleteExtension)) {
    // DuckDB's own autoload: installs it if autoinstall_known_extensions allows, then loads it.
    dd::ExtensionHelper::TryAutoLoadExtension(context, kAutoCompleteExtension);
  }
  autocomplete_loaded_ = extensions.ExtensionIsLoaded(kAutoCompleteExtension);
  if (autocomplete_loaded_) WrapAutoComplete(context);
  state_->locked.store(true);
}

bool ParseGuard::locked() const { return state_->locked.load(); }

ParseGuard::~ParseGuard() {
  // (An embedding may start another server in the process: its own guard takes over.)
  const State* mine = state_.get();
  autocomplete_state.compare_exchange_strong(mine, nullptr);
}

}  // namespace hardcharger
