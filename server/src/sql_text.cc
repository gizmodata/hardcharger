#include "sql_text.hpp"

#include <algorithm>
#include <ctime>
#include <format>
#include <regex>

#include "duckdb_v2.hpp"

namespace hardcharger {

namespace {

using TokenIterator = warpduckdb::Owned<duckdb_v2_token_iterator_handle, duckdb_v2_token_iterator_destroy>;

std::string Lower(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

bool EqualsIgnoreCase(std::string_view a, std::string_view b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](char x, char y) {
           return std::tolower(static_cast<unsigned char>(x)) == std::tolower(static_cast<unsigned char>(y));
         });
}

}  // namespace

bool Token::IsWord(std::string_view word) const {
  return (type == DUCKDB_V2_TOKEN_TYPE_KEYWORD || type == DUCKDB_V2_TOKEN_TYPE_IDENTIFIER) &&
         !text.starts_with('"') && EqualsIgnoreCase(text, word);
}

bool Token::IsOp(std::string_view op) const {
  return type == DUCKDB_V2_TOKEN_TYPE_OPERATOR && text.starts_with(op);
}

std::string Token::Value() const {
  if (type == DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL) return Unquote(text);
  if (type == DUCKDB_V2_TOKEN_TYPE_IDENTIFIER && text.starts_with('"')) return Lower(Unquote(text));
  return Lower(text);
}

std::string Unquote(std::string_view literal) {
  if (literal.size() >= 2 && literal.front() == '$') {
    const auto tag_end = literal.find('$', 1);
    if (tag_end != std::string_view::npos) {
      const std::string_view tag = literal.substr(0, tag_end + 1);
      if (literal.size() >= 2 * tag.size() && literal.ends_with(tag)) {
        return std::string(literal.substr(tag.size(), literal.size() - 2 * tag.size()));
      }
    }
    return std::string(literal);
  }
  bool escapes = false;
  if (literal.size() >= 3 && (literal.front() == 'E' || literal.front() == 'e') && literal[1] == '\'') {
    escapes = true;
    literal.remove_prefix(1);
  }
  if (literal.size() < 2) return std::string(literal);
  const char quote = literal.front();
  if ((quote != '\'' && quote != '"') || literal.back() != quote) return std::string(literal);
  std::string out;
  for (std::size_t i = 1; i + 1 < literal.size(); i++) {
    const char c = literal[i];
    if (escapes && c == '\\' && i + 2 < literal.size()) {
      out += literal[++i];
    } else if (c == quote && i + 2 < literal.size() && literal[i + 1] == quote) {
      out += quote;
      i++;
    } else {
      out += c;
    }
  }
  return out;
}

std::vector<Token> Tokenize(duckdb_v2_connection_handle connection, std::string_view sql) {
  const duckdb_v2_str text{sql.data(), sql.size()};
  TokenIterator iterator;
  warpduckdb::Call([&](auto* err) { return duckdb_v2_tokenize_sql(connection, &text, iterator.out(), err); });
  std::vector<Token> tokens;
  for (;;) {
    DUCKDB_V2_TOKEN_TYPE type = DUCKDB_V2_TOKEN_TYPE_INVALID;
    idx_t start = 0;
    idx_t length = 0;
    warpduckdb::Call([&](auto* err) {
      return duckdb_v2_token_iterator_next(iterator.get(), &type, &start, &length, err);
    });
    if (type == DUCKDB_V2_TOKEN_TYPE_END_OF_INPUT) return tokens;
    tokens.push_back({type, sql.substr(start, length)});
  }
}

std::vector<Piece> SplitStatements(duckdb_v2_connection_handle connection, std::string_view sql) {
  std::vector<Piece> pieces;
  Piece current;
  auto flush = [&] {
    if (!current.tokens.empty()) {
      const char* begin = current.tokens.front().text.data();
      const auto& last = current.tokens.back().text;
      current.text = std::string_view(begin, static_cast<std::size_t>(last.data() + last.size() - begin));
      pieces.push_back(std::move(current));
    }
    current = Piece{};
  };
  for (const Token& token : Tokenize(connection, sql)) {
    if (token.Is(DUCKDB_V2_TOKEN_TYPE_TERMINATOR)) {
      flush();
    } else if (!token.Is(DUCKDB_V2_TOKEN_TYPE_COMMENT)) {
      current.tokens.push_back(token);
    }
  }
  flush();
  return pieces;
}

std::optional<SettingCommand> ParseSettingCommand(const Piece& piece) {
  const auto& t = piece.tokens;
  std::size_t i = 0;
  SettingCommand command;
  if (i < t.size() && t[i].IsWord("RESET")) {
    command.reset = true;
  } else if (!(i < t.size() && t[i].IsWord("SET"))) {
    return std::nullopt;
  }
  i++;
  if (i < t.size() && (t[i].IsWord("SESSION") || t[i].IsWord("LOCAL"))) {
    command.scope = SettingCommand::Scope::kSession;
    i++;
  } else if (i < t.size() && t[i].IsWord("GLOBAL")) {
    command.scope = SettingCommand::Scope::kGlobal;
    i++;
  }
  // prefix '.' name. (DuckDB's tokenizer reports a lone '.' as a number literal, so it
  // is matched by its text.)
  if (i >= t.size()) return std::nullopt;
  std::string name = t[i].Value();
  i++;
  while (i + 1 < t.size() && t[i].text == ".") {
    name += "." + t[i + 1].Value();
    i += 2;
  }
  const std::string prefix = "hardcharger.";
  if (!name.starts_with(prefix) || name.size() == prefix.size()) return std::nullopt;
  command.name = name.substr(prefix.size());
  if (command.reset) {
    if (i != t.size()) return std::nullopt;
    return command;
  }
  if (i < t.size() && (t[i].IsOp("=") || t[i].IsWord("TO"))) {
    i++;
  } else {
    return std::nullopt;
  }
  if (i >= t.size()) {
    command.value.clear();
    return command;
  }
  if (i + 1 == t.size() && t[i].Is(DUCKDB_V2_TOKEN_TYPE_STRING_LITERAL)) {
    command.value = Unquote(t[i].text);
  } else {
    const char* begin = t[i].text.data();
    const auto& last = t.back().text;
    command.value = std::string(begin, static_cast<std::size_t>(last.data() + last.size() - begin));
  }
  return command;
}

std::optional<std::string> ParseKillSession(const Piece& piece) {
  if (piece.tokens.size() < 2 || !piece.tokens[0].IsWord("KILL") || !piece.tokens[1].IsWord("SESSION")) {
    return std::nullopt;
  }
  static const std::regex kKill(R"re(^\s*KILL\s+SESSION\s+(?:'([^']*)'|"([^"]*)"|([0-9A-Za-z_-]+))\s*$)re",
                                std::regex::icase);
  std::match_results<std::string_view::const_iterator> match;
  if (!std::regex_match(piece.text.begin(), piece.text.end(), match, kKill)) {
    return std::string();  // a KILL SESSION without a usable id
  }
  for (int group = 1; group <= 3; group++) {
    if (match[group].matched) return match[group].str();
  }
  return std::string();
}

namespace {

std::string Doubled(std::string_view text, char quote) {
  std::string out(1, quote);
  for (char c : text) {
    out += c;
    if (c == quote) out += quote;
  }
  out += quote;
  return out;
}

}  // namespace

std::string SqlLiteral(std::string_view text) { return Doubled(text, '\''); }

std::string SqlIdentifier(std::string_view name) { return Doubled(name, '"'); }

std::string SqlTimestamp(std::chrono::system_clock::time_point time) {
  const auto us = std::chrono::duration_cast<std::chrono::microseconds>(time.time_since_epoch()).count();
  const std::time_t seconds = static_cast<std::time_t>(us / 1000000);
  std::tm utc{};
  gmtime_r(&seconds, &utc);
  char buffer[32];
  const std::size_t length = std::strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &utc);
  return std::format("'{}.{:06}+00'::TIMESTAMPTZ", std::string_view(buffer, length), us % 1000000);
}

}  // namespace hardcharger
