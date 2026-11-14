#include "redact.hpp"

#include <algorithm>
#include <cctype>
#include <regex>

namespace hardcharger {

namespace {

// What a credential's name ends in. One list, for the SQL names RedactSql looks at and the
// key=value text RedactCredentials reads.
constexpr std::string_view kCredentialNames[] = {
    "password", "passwd",  "pwd",     "passphrase",  "secret",         "token", "access_key", "accountkey",
    "account_key", "apikey", "api_key", "private_key", "encryption_key", "sig",   "signature"};

std::string NamesPattern() {
  std::string pattern;
  for (const auto name : kCredentialNames) {
    if (!pattern.empty()) pattern += '|';
    pattern += name;
  }
  return pattern;
}

std::string Lower(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

}  // namespace

bool IsCredentialName(std::string_view name) {
  return std::any_of(std::begin(kCredentialNames), std::end(kCredentialNames),
                     [&](std::string_view known) { return name.ends_with(known); });
}

std::string RedactCredentials(std::string text) {
  // The value of a credential key: quoted (libpq's backslash escapes understood; a quote
  // that never closes runs to the end), or up to whitespace, a quote or the end of a
  // clause. (A URL's '&' does not end it: in libpq's keyword form '&' may be part of a
  // password, so the rest of a query string goes too rather than part of a password stay.)
  static const std::regex kPair(
      "((?:" + NamesPattern() + R"(|x-amz-[a-z-]+)\s*(?:=>|:=|=)\s*)([eE]?'[^'\\]*(?:\\.[^'\\]*)*'?|"[^"]*"|[^\s"';)]+))",
      std::regex::icase | std::regex::optimize);
  static const std::regex kQuoted("('[a-z0-9_.-]*(?:" + NamesPattern() + R"()'\s*:\s*)('[^']*'))",
                                  std::regex::icase | std::regex::optimize);
  // A URL's ?key=... (an API key's usual parameter), up to the next parameter.
  static const std::regex kQueryKey(R"(([?&]key=)[^&\s"']+)", std::regex::icase | std::regex::optimize);
  // user:password@ in an authority, the password up to the last '@' before the host.
  static const std::regex kAuthority(R"((://[^:/@\s"']*:)[^\s"']*@)", std::regex::optimize);
  text = std::regex_replace(text, kPair, "$1***");
  text = std::regex_replace(text, kQuoted, "$1'***'");
  text = std::regex_replace(text, kQueryKey, "$1***");
  text = std::regex_replace(text, kAuthority, "$1***@");
  return text;
}

std::string RedactCredentials(std::string text, const std::vector<std::string>& secrets) {
  for (const auto& secret : secrets) {
    if (secret.empty()) continue;
    for (std::size_t at; (at = text.find(secret)) != std::string::npos;) {
      text.replace(at, secret.size(), "<connection string>");
    }
  }
  return RedactCredentials(std::move(text));
}

namespace {

// ---- SQL as tokens -----------------------------------------------------------------

enum class Kind {
  kSpace,
  kComment,
  kString,      // '..', E'..', x'..' (any prefix), $tag$..$tag$
  kQuotedName,  // "..", `..`
  kWord,        // a keyword, name (dots included: s.t), number or $1
  kOperator,    // a run of operator characters: =, =>, :=, ::, ||
  kOpen,        // ( [ {
  kClose,       // ) ] }
  kComma,
  kSemicolon,
  kEnd,
  kUnreadable,  // a string, quoted name or block comment that never ends
};

struct Lexeme {
  Kind kind;
  std::string_view text;
};

bool IsWordChar(char c) {
  const auto u = static_cast<unsigned char>(c);
  return std::isalnum(u) || c == '_' || c == '.' || u >= 0x80;
}

class Lexer {
 public:
  explicit Lexer(std::string_view sql) : sql_(sql) {}

  Lexeme Next() {
    const std::size_t n = sql_.size();
    const std::size_t start = at_;
    if (at_ >= n) return {Kind::kEnd, {}};
    const char c = sql_[at_];
    auto made = [&](Kind kind) { return Lexeme{kind, sql_.substr(start, at_ - start)}; };
    if (std::isspace(static_cast<unsigned char>(c))) {
      while (at_ < n && std::isspace(static_cast<unsigned char>(sql_[at_]))) at_++;
      return made(Kind::kSpace);
    }
    if (StartsComment(at_)) {
      if (c == '-') {  // to the end of the line, however it ends
        while (at_ < n && sql_[at_] != '\n' && sql_[at_] != '\r') at_++;
        return made(Kind::kComment);
      }
      int depth = 0;  // block comments nest
      do {
        if (at_ + 1 >= n) return Unreadable();
        if (sql_[at_] == '/' && sql_[at_ + 1] == '*') {
          depth++;
          at_ += 2;
        } else if (sql_[at_] == '*' && sql_[at_ + 1] == '/') {
          depth--;
          at_ += 2;
        } else {
          at_++;
        }
      } while (depth > 0);
      return made(Kind::kComment);
    }
    if (c == '\'') return String(start, /*escapes=*/false);
    if (c == '"' || c == '`') {
      for (at_++;; at_++) {
        if (at_ >= n) return Unreadable();
        if (sql_[at_] != c) continue;
        if (at_ + 1 < n && sql_[at_ + 1] == c) {
          at_++;
          continue;
        }
        at_++;
        return made(Kind::kQuotedName);
      }
    }
    if (c == '$') {
      // $$..$$ or $tag$..$tag$ (a tag is a name, so $1 is a parameter, not a tag).
      std::size_t tag_end = at_ + 1;
      if (tag_end < n && IsWordChar(sql_[tag_end]) && !std::isdigit(static_cast<unsigned char>(sql_[tag_end]))) {
        while (tag_end < n && IsWordChar(sql_[tag_end]) && sql_[tag_end] != '.') tag_end++;
      }
      if (tag_end < n && sql_[tag_end] == '$') {
        const std::string_view tag = sql_.substr(at_, tag_end - at_ + 1);
        const std::size_t close = sql_.find(tag, tag_end + 1);
        if (close == std::string_view::npos) return Unreadable();
        at_ = close + tag.size();
        return made(Kind::kString);
      }
      at_++;
      while (at_ < n && IsWordChar(sql_[at_])) at_++;
      return made(Kind::kWord);
    }
    if (IsWordChar(c)) {
      while (at_ < n && IsWordChar(sql_[at_])) at_++;
      // A literal with a one-letter prefix (E'..', x'..', b'..'): one token. Only E'..'
      // has backslash escapes. Any other word before a quote is a word of its own
      // (PASSWORD'..' is a name and its value).
      const bool prefix = at_ - start == 1 && std::string_view("eExXbBnN").find(c) != std::string_view::npos;
      if (prefix && at_ < n && sql_[at_] == '\'') return String(start, c == 'e' || c == 'E');
      return made(Kind::kWord);
    }
    at_++;
    if (c == '(' || c == '[' || c == '{') return made(Kind::kOpen);
    if (c == ')' || c == ']' || c == '}') return made(Kind::kClose);
    if (c == ',') return made(Kind::kComma);
    if (c == ';') return made(Kind::kSemicolon);
    while (at_ < n && IsOperatorChar(sql_[at_]) && !StartsComment(at_)) at_++;
    return made(Kind::kOperator);
  }

 private:
  bool StartsComment(std::size_t at) const {
    return at + 1 < sql_.size() &&
           ((sql_[at] == '-' && sql_[at + 1] == '-') || (sql_[at] == '/' && sql_[at + 1] == '*'));
  }

  static bool IsOperatorChar(char c) {
    return !IsWordChar(c) && !std::isspace(static_cast<unsigned char>(c)) &&
           std::string_view("'\"`$()[]{},;").find(c) == std::string_view::npos;
  }

  // The literal whose opening quote is at at_ (after any prefix, which starts at `start`).
  Lexeme String(std::size_t start, bool escapes) {
    const std::size_t n = sql_.size();
    for (at_++;;) {
      if (at_ >= n) return Unreadable();
      const char c = sql_[at_];
      if (escapes && c == '\\') {
        at_ += 2;
      } else if (c == '\'' && at_ + 1 < n && sql_[at_ + 1] == '\'') {
        at_ += 2;
      } else if (c == '\'') {
        at_++;
        return {Kind::kString, sql_.substr(start, at_ - start)};
      } else {
        at_++;
      }
    }
  }

  Lexeme Unreadable() {
    at_ = sql_.size();
    return {Kind::kUnreadable, {}};
  }

  std::string_view sql_;
  std::size_t at_ = 0;
};

// What a string literal or a quoted name holds: quotes, prefix and doubling removed.
std::string Body(std::string_view token) {
  if (token.starts_with('$')) {
    const std::size_t tag = token.find('$', 1) + 1;
    return std::string(token.substr(tag, token.size() - 2 * tag));
  }
  const std::size_t open = token.find_first_of("'\"`");
  const char quote = token[open];
  const bool escapes = open == 1 && (token[0] == 'e' || token[0] == 'E');
  std::string out;
  for (std::size_t i = open + 1; i + 1 < token.size(); i++) {
    if (escapes && token[i] == '\\' && i + 2 < token.size()) {
      out += token[++i];
    } else {
      out += token[i];
      if (token[i] == quote) i++;  // a doubled quote
    }
  }
  return out;
}

std::string Quoted(std::string_view body, char quote) {
  std::string out(1, quote);
  for (const char c : body) {
    out += c;
    if (c == quote) out += quote;
  }
  out += quote;
  return out;
}

// A literal or quoted name with the credentials inside it (a connection string's, a URL's)
// masked; as written when it holds none.
std::string MaskedInside(std::string_view token, char quote) {
  if (token.find('=') == std::string_view::npos && token.find("://") == std::string_view::npos) {
    return std::string(token);
  }
  const std::string body = Body(token);
  std::string masked = RedactCredentials(body);
  return masked == body ? std::string(token) : Quoted(masked, quote);
}

// A quoted name, or a literal used as a key, that is a credential's. (No name is long.)
bool IsName(std::string_view token) { return token.size() <= 80 && IsCredentialName(Lower(Body(token))); }

// A comment as it may be logged: it can hold a statement of its own (one commented out).
std::string MaskedComment(std::string_view comment) { return RedactCredentials(std::string(comment)); }

constexpr std::string_view kMask = "***";
constexpr std::string_view kCut = "\xe2\x80\xa6";  // …

// One pass over the statement's tokens: writes the loggable text (when `text` is given)
// and collects the masked values (when `values` is). False when it cannot be read.
bool Scan(std::string_view sql, std::size_t limit, std::string* text, std::vector<std::string>* values) {
  Lexer lexer(sql);
  std::optional<Lexeme> held;  // read while finding a value's end; handled next
  auto next = [&] {
    if (!held) return lexer.Next();
    const Lexeme lexeme = *held;
    held.reset();
    return lexeme;
  };
  bool cut = false;
  auto fits = [&](std::size_t bytes) { return text == nullptr || text->size() + bytes <= limit; };
  auto emit = [&](std::string_view piece) {
    if (text == nullptr || cut) return;
    if (!fits(piece.size())) {
      *text += kCut;
      cut = true;
      return;
    }
    *text += piece;
  };

  // How far a credential's value runs: to the end of what it is an argument or an operand
  // of. Unsure means further (more is masked, never less).
  enum class Shape {
    kArgument,  // after =>, := or a struct key's colon: to the next , ) or ;
    kOperand,   // after = or a comparison, or an option list's NAME 'v': the same, or to the
                // word that starts the next clause (x = 'v' AND ..., ... WHERE ...)
    kGroup,     // a function's whole argument list: to its )
  };
  static constexpr std::string_view kNextClause[] = {
      "and",   "or",     "where",     "from",      "group", "order", "having", "limit", "offset", "union",
      "except", "intersect", "returning", "on",       "join",  "using", "as",     "window", "qualify"};
  // Consumes a value and writes *** for it. False when the statement cannot be read.
  auto mask_value = [&](Shape shape) {
    int depth = 0;          // brackets open inside the value
    int cases = 0;          // CASE ... END open inside it (its AND and OR are the value's)
    int parts = 0;
    std::string_view only;  // the value, while it is a single word or operator
    std::string spaces;     // after the value's last token: kept
    for (;;) {
      const Lexeme l = next();
      if (l.kind == Kind::kUnreadable) return false;
      if (l.kind == Kind::kSpace || l.kind == Kind::kComment) {
        // Before the value they are kept; inside or after it a comment goes with it.
        if (parts == 0) {
          emit(l.kind == Kind::kComment && fits(l.text.size()) ? MaskedComment(l.text) : std::string(l.text));
        } else if (l.kind == Kind::kSpace) {
          spaces += l.text;
        }
        continue;
      }
      bool ends = l.kind == Kind::kEnd;
      if (!ends && depth == 0) {
        ends = l.kind == Kind::kClose || l.kind == Kind::kSemicolon ||
               (l.kind == Kind::kComma && shape != Shape::kGroup);
        if (shape == Shape::kOperand && l.kind == Kind::kWord) {
          const std::string word = Lower(l.text);
          if (word == "case") {
            cases++;
          } else if (word == "end" && cases > 0) {
            cases--;
          } else if (parts > 0 && cases == 0) {
            ends = std::find(std::begin(kNextClause), std::end(kNextClause), word) != std::end(kNextClause);
          }
        }
      }
      if (ends) {
        held = l;
        break;
      }
      if (l.kind == Kind::kOpen) depth++;
      if (l.kind == Kind::kClose) depth--;
      only = parts++ == 0 && l.kind != Kind::kString ? l.text : std::string_view();
      spaces.clear();
      if (l.kind == Kind::kString && values != nullptr) {
        values->emplace_back(l.text);
        if (std::string body = Body(l.text); body.size() >= 4) values->push_back(std::move(body));
      }
    }
    if (parts > 0) {
      const bool placeholder = only == "?" || (only.size() > 1 && only.starts_with('$'));
      emit(placeholder ? only : kMask);
      emit(spaces);
    }
    return true;
  };

  enum class After { kNothing, kName, kLiteralKey, kKeyFunction };
  After after = After::kNothing;
  bool first = true;    // the next token starts a statement
  bool attach = false;  // in an ATTACH, whose options take a bare word as a value: (TOKEN abc)
  while (!cut) {
    const Lexeme l = next();
    if (l.kind == Kind::kUnreadable) return false;
    if (l.kind == Kind::kEnd) break;
    if (l.kind == Kind::kSpace || l.kind == Kind::kComment) {
      emit(l.kind == Kind::kComment && fits(l.text.size()) ? MaskedComment(l.text) : std::string(l.text));
      continue;
    }
    if (first) attach = l.kind == Kind::kWord && Lower(l.text) == "attach";
    first = l.kind == Kind::kSemicolon;
    const After was = after;
    after = After::kNothing;
    if (was != After::kNothing) {
      std::optional<Shape> shape;
      if (l.kind == Kind::kOperator && was != After::kKeyFunction) {
        const bool comparison = l.text.starts_with('=') || l.text.starts_with('<') || l.text.starts_with('>') ||
                                l.text.starts_with("!=");
        if (l.text.starts_with("=>") || (l.text.starts_with(':') && !l.text.starts_with("::"))) {
          shape = Shape::kArgument;
        } else if (comparison && was == After::kName) {
          shape = Shape::kOperand;
        }
        if (shape) emit(l.text);
      } else if (was == After::kName &&
                 (l.kind == Kind::kString || (attach && (l.kind == Kind::kWord || l.kind == Kind::kQuotedName)))) {
        shape = Shape::kOperand;  // an option list's NAME 'value'
        held = l;
      } else if (l.kind == Kind::kOpen && l.text == "(" && was == After::kKeyFunction) {
        shape = Shape::kGroup;
        emit(l.text);
      }
      if (shape) {
        if (!mask_value(*shape)) return false;
        continue;
      }
    }
    // A token longer than what may still be written is never looked into.
    if (!fits(l.text.size())) {
      emit(l.text);
      break;
    }
    if (l.kind == Kind::kWord) {
      // (A no-break space, which DuckDB reads as a space, is no part of the name.)
      std::string name = Lower(l.text);
      while (!name.empty() && static_cast<unsigned char>(name.back()) >= 0x80) name.pop_back();
      // PRAGMA add_parquet_key('name', '<key>'): the key is an argument without a name.
      after = IsCredentialName(name) ? After::kName : name == "add_parquet_key" ? After::kKeyFunction : After::kNothing;
      emit(l.text);
    } else if (l.kind == Kind::kQuotedName) {
      if (IsName(l.text)) after = After::kName;
      emit(text != nullptr ? MaskedInside(l.text, '"') : std::string());
    } else if (l.kind == Kind::kString) {
      if (IsName(l.text)) after = After::kLiteralKey;  // {'password': '..'}
      emit(text != nullptr ? MaskedInside(l.text, '\'') : std::string());
    } else {
      emit(l.text);
    }
  }
  return true;
}

}  // namespace

RedactedSql RedactSql(std::string_view sql, std::size_t limit) {
  RedactedSql out;
  if (!Scan(sql, limit, &out.text, nullptr)) {
    out.text.clear();
    out.unreadable = true;
  }
  return out;
}

std::optional<std::vector<std::string>> CredentialValues(std::string_view sql) {
  std::vector<std::string> values;
  if (!Scan(sql, 0, nullptr, &values)) return std::nullopt;
  return values;
}

}  // namespace hardcharger
