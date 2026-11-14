#include "redact.hpp"

#include <algorithm>
#include <cctype>

namespace hardcharger {

namespace {

// What a credential's name ends in. One list, for the SQL names RedactSql looks at and the
// key=value text RedactCredentials reads.
constexpr std::string_view kCredentialNames[] = {
    "password", "passwd",  "pwd",     "passphrase",  "secret",         "token", "access_key", "accountkey",
    "account_key", "apikey", "api_key", "private_key", "encryption_key", "sig",   "signature"};

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
  // Read key/value text iteratively: no regex recursion/backtracking on untrusted
  // error messages or connection strings. Every consumed byte is visited O(1) times.
  auto name_char = [](unsigned char c) {
    return std::isalnum(c) || c == '_' || c == '.' || c == '-';
  };
  auto spaces = [&](std::size_t at) {
    while (at < text.size() && std::isspace(static_cast<unsigned char>(text[at]))) at++;
    return at;
  };
  auto value_end = [&](std::size_t at, bool query_key) {
    if (query_key) {
      while (at < text.size() && text[at] != '&' && text[at] != '\'' && text[at] != '"' &&
             !std::isspace(static_cast<unsigned char>(text[at]))) at++;
      return at;
    }
    if (at + 1 < text.size() && (text[at] == 'e' || text[at] == 'E') && text[at + 1] == '\'') at++;
    if (at < text.size() && (text[at] == '\'' || text[at] == '"')) {
      const char quote = text[at++];
      while (at < text.size()) {
        if (text[at] == '\\') {
          at += std::min<std::size_t>(2, text.size() - at);
        } else if (text[at++] == quote) {
          if (at < text.size() && text[at] == quote) at++;
          else return at;
        }
      }
      return at;  // an unterminated value is concealed through the end
    }
    // '&' may be part of a libpq password; conservatively conceal the remainder
    // of that query string as well, matching the connection-string policy.
    while (at < text.size()) {
      // libpq permits a backslash to escape any following byte, including a
      // delimiter. Consume the pair before considering the value's boundary.
      if (text[at] == '\\') {
        at += std::min<std::size_t>(2, text.size() - at);
      } else if (std::isspace(static_cast<unsigned char>(text[at])) ||
                 std::string_view("\"';)").find(text[at]) != std::string_view::npos) {
        break;
      } else {
        at++;
      }
    }
    return at;
  };
  std::string pairs;
  pairs.reserve(text.size());
  for (std::size_t at = 0; at < text.size();) {
    const std::size_t start = at;
    const bool quoted = text[at] == '\'' || text[at] == '"';
    const char quote = text[at];
    const std::size_t name_start = at + (quoted ? 1 : 0);
    std::size_t name_end = name_start;
    while (name_end < text.size() && name_char(text[name_end])) name_end++;
    if (name_end == name_start) {
      pairs += text[at++];
      continue;
    }
    const std::string name = Lower(std::string_view(text).substr(name_start, name_end - name_start));
    const bool query_key = !quoted && name == "key" && start > 0 && (text[start - 1] == '?' || text[start - 1] == '&');
    const bool credential = IsCredentialName(name) || name.starts_with("x-amz-") || query_key;
    std::size_t op = spaces(name_end);
    bool assignment = false;
    if (quoted && name_end < text.size() && text[name_end] == quote) {
      op = spaces(name_end + 1);
      assignment = op < text.size() && text[op] == ':';
      if (assignment) op++;
    } else if (!quoted && op < text.size()) {
      if (text[op] == '=') {
        op++;
        if (op < text.size() && text[op] == '>') op++;
        assignment = true;
      } else if (text[op] == ':' && op + 1 < text.size() && text[op + 1] == '=') {
        op += 2;
        assignment = true;
      }
    }
    if (credential && assignment) {
      const auto value = spaces(op);
      const auto end = value_end(value, query_key);
      pairs.append(text, start, value - start);
      pairs += quoted ? "'***'" : "***";
      at = end;
    } else {
      // At a quote leave its body available to recognize embedded connection text.
      const auto end = quoted ? start + 1 : name_end;
      pairs.append(text, start, end - start);
      at = end;
    }
  }
  text = std::move(pairs);
  // Each whitespace/quote-delimited segment is scanned once. In particular, repeated
  // malformed ://user: prefixes without @ must not retry every remaining suffix.
  std::string out;
  out.reserve(text.size());
  for (std::size_t start = 0; start < text.size();) {
    auto delimiter = [](unsigned char c) { return std::isspace(c) || c == '\'' || c == '"'; };
    if (delimiter(text[start])) {
      out += text[start++];
      continue;
    }
    std::size_t end = start;
    while (end < text.size() && !delimiter(text[end])) end++;
    const std::string_view segment(text.data() + start, end - start);
    const auto last_at = segment.rfind('@');
    std::size_t password = std::string_view::npos;
    if (last_at != std::string_view::npos) {
      for (std::size_t i = 0; i + 3 < last_at; i++) {
        if (segment.substr(i, 3) != "://") continue;
        std::size_t colon = i + 3;
        while (colon < last_at && segment[colon] != ':' && segment[colon] != '/' && segment[colon] != '@') colon++;
        if (colon < last_at && segment[colon] == ':') {
          password = colon + 1;
          break;
        }
        i = colon;
      }
    }
    if (password == std::string_view::npos) {
      out += segment;
    } else {
      out += segment.substr(0, password);
      out += "***";
      out += segment.substr(last_at);
    }
    start = end;
  }
  return out;
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

// DuckDB normalizes Unicode space characters before parsing. Recognize their UTF-8
// encodings before identifiers, including when a space follows a quoted name.
std::size_t SpaceBytes(std::string_view text, std::size_t at) {
  if (at >= text.size()) return 0;
  if (std::isspace(static_cast<unsigned char>(text[at]))) return 1;
  const auto tail = text.substr(at);
  if (tail.starts_with("\xc2\xa0")) return 2;
  if (tail.size() < 3) return 0;
  const auto last = static_cast<unsigned char>(tail[2]);
  if (tail.starts_with("\xe1\x9a\x80") || tail.starts_with("\xe2\x81\x9f") ||
      tail.starts_with("\xe3\x80\x80") || tail.starts_with("\xef\xbb\xbf") ||
      (tail.starts_with("\xe2\x80") && ((last >= 0x80 && last <= 0x8b) || last == 0xa8 || last == 0xa9 || last == 0xaf))) return 3;
  return 0;
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
    if (SpaceBytes(sql_, at_) != 0) {
      while (const auto bytes = SpaceBytes(sql_, at_)) at_ += bytes;
      return made(Kind::kSpace);
    }
    if (StartsComment(at_)) {
      if (c == '-') {  // to the end of the line, however it ends
        while (at_ < n && sql_[at_] != '\n' && sql_[at_] != '\r') at_++;
        return made(Kind::kComment);
      }
      std::size_t depth = 0;  // block comments nest
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
        while (tag_end < n && IsWordChar(sql_[tag_end]) && sql_[tag_end] != '.' && SpaceBytes(sql_, tag_end) == 0) tag_end++;
      }
      if (tag_end < n && sql_[tag_end] == '$') {
        const std::string_view tag = sql_.substr(at_, tag_end - at_ + 1);
        const std::size_t close = sql_.find(tag, tag_end + 1);
        if (close == std::string_view::npos) return Unreadable();
        at_ = close + tag.size();
        return made(Kind::kString);
      }
      at_++;
      while (at_ < n && IsWordChar(sql_[at_]) && SpaceBytes(sql_, at_) == 0) at_++;
      return made(Kind::kWord);
    }
    if (IsWordChar(c)) {
      while (at_ < n && IsWordChar(sql_[at_]) && SpaceBytes(sql_, at_) == 0) at_++;
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

// Escaped strings can encode credential names or connection strings. Treat them
// conservatively, without trying to duplicate DuckDB's escape decoder.
bool Escaped(std::string_view token) {
  return token.size() > 2 && (token[0] == 'e' || token[0] == 'E') && token[1] == '\'' &&
         token.find('\\') != std::string_view::npos;
}

std::string MaskedInside(std::string_view token, char quote, bool& credentials) {
  if (Escaped(token)) {
    credentials = true;
    return "'***'";
  }
  // The helper also recognizes quoted JSON/struct keys followed by a colon.
  if (token.find_first_of("=:") == std::string_view::npos) {
    return std::string(token);
  }
  const std::string body = Body(token);
  std::string masked = RedactCredentials(body);
  if (masked == body) return std::string(token);
  credentials = true;
  return Quoted(masked, quote);
}

bool IsName(std::string_view token) { return Escaped(token) || IsCredentialName(Lower(Body(token))); }

bool IsKeyFunction(std::string_view name) {
  return name == "add_parquet_key" || name.ends_with(".add_parquet_key");
}

// Comments may hold arbitrary commented-out credential expressions. Their bodies
// are never retained; marking them sensitive also keeps them out of errors/profiles.
std::string_view MaskedComment(std::string_view comment) {
  return comment.starts_with("--") ? "-- ***" : "/* *** */";
}

constexpr std::string_view kMask = "***";
constexpr std::string_view kCut = "\xe2\x80\xa6";  // …

// One pass over the statement's tokens: writes the loggable text (when `text` is given)
// and identifies sensitive content independently of the display limit. False if unreadable.
bool Scan(std::string_view sql, std::size_t limit, std::string* text, bool& credentials) {
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
    std::size_t depth = 0;  // brackets open inside the value
    std::size_t cases = 0;          // CASE ... END open inside it (its AND and OR are the value's)
    std::size_t parts = 0;
    std::string_view only;  // the value, while it is a single word or operator
    std::string spaces;     // after the value's last token: kept
    for (;;) {
      const Lexeme l = next();
      if (l.kind == Kind::kUnreadable) return false;
      if (l.kind == Kind::kSpace || l.kind == Kind::kComment) {
        // Before the value they are kept; inside or after it a comment goes with it.
        if (parts == 0) {
          if (l.kind == Kind::kComment) credentials = true;
          emit(l.kind == Kind::kComment ? MaskedComment(l.text) : l.text);
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
  while (true) {
    const Lexeme l = next();
    if (l.kind == Kind::kUnreadable) return false;
    if (l.kind == Kind::kEnd) break;
    if (l.kind == Kind::kSpace || l.kind == Kind::kComment) {
      if (l.kind == Kind::kComment) credentials = true;
      emit(l.kind == Kind::kComment ? MaskedComment(l.text) : l.text);
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
        credentials = true;  // includes placeholders, bare values and empty/malformed values
        if (!mask_value(*shape)) return false;
        continue;
      }
    }
    if (l.kind == Kind::kWord) {
      const std::string name = Lower(l.text);
      // PRAGMA add_parquet_key('name', '<key>'): the key is an argument without a name.
      after = IsCredentialName(name) ? After::kName : IsKeyFunction(name) ? After::kKeyFunction : After::kNothing;
      emit(l.text);
    } else if (l.kind == Kind::kQuotedName) {
      if (IsName(l.text)) after = After::kName;
      else if (IsKeyFunction(Lower(Body(l.text)))) after = After::kKeyFunction;
      emit(MaskedInside(l.text, '"', credentials));
    } else if (l.kind == Kind::kString) {
      if (IsName(l.text)) after = After::kLiteralKey;  // {'password': '..'}
      emit(MaskedInside(l.text, '\'', credentials));
    } else {
      emit(l.text);
    }
  }
  return true;
}

}  // namespace

RedactedSql RedactSql(std::string_view sql, std::size_t limit) {
  RedactedSql out;
  bool credentials = false;
  if (!Scan(sql, limit, &out.text, credentials)) {
    out.text.clear();
    out.unreadable = true;
  }
  return out;
}

std::optional<bool> SqlHasCredentials(std::string_view sql) {
  bool credentials = false;
  if (!Scan(sql, 0, nullptr, credentials)) return std::nullopt;
  return credentials;
}

}  // namespace hardcharger
