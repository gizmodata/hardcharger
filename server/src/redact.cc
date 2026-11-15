#include "redact.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <functional>

#include "sql_lex.hpp"

namespace hardcharger {

namespace {

constexpr std::string_view kMask = "***";
constexpr std::string_view kCut = "\xe2\x80\xa6";  // …

// What a credential's name ends in, in lower case with '_' for '-'. One list, for the names
// MaskSql looks at in a statement and the keys RedactCredentials reads in other text.
constexpr std::string_view kCredentialNames[] = {
    "password",    "passwd",       "pwd",            "passphrase",       "secret",    "token",
    "access_key",  "secret_key",   "accountkey",     "account_key",      "apikey",    "api_key",
    "private_key", "encryption_key", "subscription_key", "sig",          "signature", "auth",
    "authorization", "credential", "credentials",    "cookie",           "jwt",       "bearer"};

// Parameters of a URL's query that are credentials under these exact names as well.
constexpr std::string_view kQueryCredentialNames[] = {"key", "code", "sessionid", "session_id"};

// HTTP headers that are credentials, as `Name: value` text.
constexpr std::string_view kHeaderNames[] = {"authorization", "proxy_authorization", "cookie", "set_cookie",
                                             "x_api_key",     "x_auth_token",        "x_amz_security_token"};

// A setting is a credential setting when its name has one of these anywhere in it.
constexpr std::string_view kSettingWords[] = {
    "password",   "passwd",      "pwd",            "passphrase", "secret",     "token",         "access_key",
    "accountkey", "account_key", "apikey",         "api_key",    "private_key", "encryption_key", "signature",
    "credential", "bearer",      "authorization",  "cookie",     "jwt"};

// URL schemes whose user is an account name, shown, with only the password masked. For any
// other scheme with a password, and for http and https always, the whole userinfo goes.
constexpr std::string_view kHttpSchemes[] = {"http", "https"};

template <std::size_t N>
bool Among(const std::string_view (&list)[N], std::string_view word) {
  return std::find(std::begin(list), std::end(list), word) != std::end(list);
}

std::string Lower(std::string_view text) {
  std::string out(text);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) { return std::tolower(c); });
  return out;
}

// A name as the lists spell it: lower case, '-' as '_'.
std::string Normal(std::string_view name) {
  std::string out = Lower(name);
  std::replace(out.begin(), out.end(), '-', '_');
  return out;
}

bool IsSpace(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f'; }
bool IsAlnum(char c) { return std::isalnum(static_cast<unsigned char>(c)) != 0; }
bool IsNameChar(char c) { return IsAlnum(c) || c == '_' || c == '.' || c == '-'; }
bool IsIdentStart(char c) { return std::isalpha(static_cast<unsigned char>(c)) != 0 || c == '_'; }
bool IsSchemeChar(char c) { return IsAlnum(c) || c == '+' || c == '.' || c == '-'; }

bool AllStars(std::string_view text) {
  return !text.empty() && text.find_first_not_of('*') == std::string_view::npos;
}

bool EndsInCredentialName(std::string_view normal) {
  // (pass and pw only as whole words: user_pass, db_pw, not bypass.)
  if (normal == "pass" || normal == "pw" || normal.ends_with("_pass") || normal.ends_with("_pw")) return true;
  return std::any_of(std::begin(kCredentialNames), std::end(kCredentialNames),
                     [&](std::string_view known) { return normal.ends_with(known); });
}

// `Bearer <token>` or `Basic <base64>` and nothing else: an Authorization header's value,
// wherever it is written.
bool IsAuthorizationValue(std::string_view text) {
  const std::string lower = Lower(text.substr(0, 7));
  std::size_t at = lower.starts_with("bearer ") ? 7 : lower.starts_with("basic ") ? 6 : 0;
  if (at == 0 || text.size() < at + 8) return false;
  return std::all_of(text.begin() + static_cast<std::ptrdiff_t>(at), text.end(),
                     [](char c) { return IsAlnum(c) || std::string_view("._~+/=:-").find(c) != std::string_view::npos; });
}

// What masking finds besides the text it writes.
struct Found {
  std::vector<std::string> secrets;
  std::vector<MaskedParameter> parameters;
  bool credentials = false;
  bool computed = false;

  void Secret(std::string_view value) {
    if (value.empty() || AllStars(value)) return;
    if (!secrets.empty() && secrets.back() == value) return;
    if (secrets.size() >= kMostSecrets) {
      computed = true;  // too many to list: errors are not shown at all
      return;
    }
    secrets.emplace_back(value);
    // `Bearer <token>`: an error may quote the token without the scheme.
    for (const std::string_view scheme : {"bearer ", "basic ", "token ", "digest ", "negotiate ", "apikey "}) {
      if (value.size() > scheme.size() && Lower(value.substr(0, scheme.size())) == scheme) {
        Secret(value.substr(value.find_first_not_of(' ', scheme.size() - 1)));
        break;
      }
    }
  }
  static constexpr std::size_t kMostSecrets = 10000;
};

std::string PercentDecoded(std::string_view text) {
  std::string out;
  for (std::size_t i = 0; i < text.size(); i++) {
    if (text[i] == '%' && i + 2 < text.size() && std::isxdigit(static_cast<unsigned char>(text[i + 1])) &&
        std::isxdigit(static_cast<unsigned char>(text[i + 2]))) {
      const char hex[3] = {text[i + 1], text[i + 2], 0};
      out += static_cast<char>(std::strtoul(hex, nullptr, 16));
      i += 2;
    } else {
      out += text[i];
    }
  }
  return out;
}

// ---- Text that is not a statement ---------------------------------------------------
//
// One pass, left to right; every look ahead is bounded, so the cost is linear in the text
// whatever it holds.

class TextMasker {
 public:
  // `whole`: the text is one value on its own (what a string literal holds), not prose that
  // quotes one: a URL in it can run to its end.
  TextMasker(std::string_view text, Found& found, bool whole) : t_(text), n_(text.size()), found_(found), whole_(whole) {}

  // The masked text. `open`: a credential's value runs to the end of the text or holds a
  // format placeholder, so what the text is joined with is part of the value.
  std::string Run(bool* open) {
    out_.reserve(n_);
    std::size_t at = 0;
    while (at < n_) {
      const char c = t_[at];
      if (c == '\n') {
        single_quotes_ = double_quotes_ = 0;
        last_solid_ = 0;
        out_ += c;
        at++;
      } else if (c == ':' && t_.substr(at, 3) == "://" && at > 0 && IsSchemeChar(t_[at - 1])) {
        at = Url(at);
      } else if ((c == '\'' || c == '"') && at + 1 < n_ && IsNameChar(t_[at + 1])) {
        at = QuotedKey(at);
      } else if (IsNameChar(c)) {
        at = Key(at);
      } else {
        Plain(c);
        at++;
      }
    }
    if (open != nullptr) *open = open_;
    return std::move(out_);
  }

 private:
  enum class Kind { kKeyword, kArgument, kQuery, kHeader };

  // Writes a character that is no part of a key or a value.
  void Plain(char c) {
    if (c == '\'') single_quotes_++;
    if (c == '"') double_quotes_++;
    if (!IsSpace(c)) last_solid_ = c;
    out_ += c;
  }
  void Plain(std::string_view text) {
    for (const char c : text) Plain(c);
  }

  std::size_t Spaces(std::size_t at) const {
    while (at < n_ && IsSpace(t_[at]) && t_[at] != '\n') at++;
    return at;
  }
  std::size_t LineEnd(std::size_t at) const {
    const auto end = t_.find('\n', at);
    return end == std::string_view::npos ? n_ : end;
  }

  // 'key': value or "key": value.
  std::size_t QuotedKey(std::size_t at) {
    const char quote = t_[at];
    std::size_t name_end = at + 1;
    while (name_end < n_ && IsNameChar(t_[name_end])) name_end++;
    std::size_t op = name_end < n_ && t_[name_end] == quote ? Spaces(name_end + 1) : n_;
    if (op >= n_ || t_[op] != ':' || (op + 1 < n_ && t_[op + 1] == ':') ||
        !IsCredentialName(t_.substr(at + 1, name_end - at - 1))) {
      Plain(quote);  // the body may still hold a key of its own
      return at + 1;
    }
    const std::size_t value = Spaces(op + 1);
    const std::size_t end = ValueEnd(value, Kind::kArgument);
    out_.append(t_, at, value - at);
    Masked(value, end, "'***'");
    return end;
  }

  // key=value, key => value, key := value, ?key=value, Header: value.
  std::size_t Key(std::size_t at) {
    std::size_t name_end = at;
    while (name_end < n_ && IsNameChar(t_[name_end])) name_end++;
    const std::string name = Normal(t_.substr(at, name_end - at));
    const bool query = at > 0 && (t_[at - 1] == '?' || t_[at - 1] == '&');
    const bool credential = EndsInCredentialName(name) || name.starts_with("x_amz_") ||
                            (query && Among(kQueryCredentialNames, name));
    last_solid_before_key_ = last_solid_;
    std::size_t op = Spaces(name_end);
    std::optional<Kind> kind;
    if (op < n_ && t_[op] == '=') {
      op++;
      if (op < n_ && t_[op] == '>') {
        op++;
        kind = Kind::kArgument;
      } else if (op < n_ && t_[op] == '=') {
        op++;  // == compares, in text that reads like SQL: the value is one all the same
        kind = Kind::kArgument;
      } else {
        kind = query ? Kind::kQuery : Kind::kKeyword;
      }
    } else if (op + 1 < n_ && t_[op] == ':' && t_[op + 1] == '=') {
      op += 2;
      kind = Kind::kArgument;
    } else if (op == name_end && op < n_ && t_[op] == ':' && (op + 1 >= n_ || t_[op + 1] != ':') &&
               Among(kHeaderNames, name)) {
      op++;
      kind = Kind::kHeader;
    }
    if (!credential || !kind) {
      Plain(t_.substr(at, name_end - at));
      return name_end;
    }
    const std::size_t value = *kind == Kind::kQuery ? op : Spaces(op);
    const std::size_t end = ValueEnd(value, *kind);
    out_.append(t_, at, value - at);
    Masked(value, end, kMask);
    return end;
  }

  // Writes the mask for the value [value, end) and keeps what it was. `joins`: the value
  // may run on into what the text is joined with (it reaches the text's end).
  void Masked(std::size_t value, std::size_t end, std::string_view mask, bool joins = true) {
    std::string_view text = t_.substr(value, end - value);
    out_ += mask;
    last_solid_ = '*';
    if ((joins && end >= n_) || text.find("{}") != std::string_view::npos || text.find("{0}") != std::string_view::npos ||
        text.find("%s") != std::string_view::npos) {
      open_ = true;
    }
    if (text.empty() || AllStars(text) || text == "'***'") return;
    found_.credentials = true;
    // As given, and as its reader takes it: quotes off, backslash escapes and doubled quotes resolved.
    if ((text.front() == 'e' || text.front() == 'E') && text.size() > 1 && text[1] == '\'') text.remove_prefix(1);
    const char quote = text.front();
    std::string resolved;
    if (quote == '\'' || quote == '"' || quote == '{') {
      const char close = quote == '{' ? '}' : quote;
      const std::size_t last = text.size() > 1 && text.back() == close ? text.size() - 1 : text.size();
      found_.Secret(text.substr(1, last - 1));
      for (std::size_t i = 1; i < last; i++) {
        if (text[i] == '\\' && quote != '{' && i + 1 < last) {
          resolved += text[++i];
        } else {
          resolved += text[i];
          if (text[i] == close && i + 1 < last && text[i + 1] == close) i++;
        }
      }
    } else {
      found_.Secret(text);
      for (std::size_t i = 0; i < text.size(); i++) {
        if (text[i] == '\\' && i + 1 < text.size()) i++;
        resolved += text[i];
      }
    }
    found_.Secret(resolved);
    found_.Secret(PercentDecoded(resolved));
  }

  // Where a quoted value ends: after the closing quote (a doubled one and a backslash's
  // character are the value's), or the end of the text when it never closes.
  // `doubling`: whether a doubled closing quote is one of the value's characters ('it''s',
  // ODBC's {a}}b}, ADO's "a""b") or the value's end followed by another quote (libpq and
  // MySQL escape with a backslash only).
  std::size_t QuotedEnd(std::size_t at, char open, bool doubling = true) const {
    const char close = open == '{' ? '}' : open;
    for (at++; at < n_;) {
      if (t_[at] == '\\' && open != '{') {
        at += std::min<std::size_t>(2, n_ - at);
      } else if (t_[at++] == close) {
        if (doubling && at < n_ && t_[at] == close) at++;
        else return at;
      }
    }
    return n_;
  }

  // An unquoted value read as libpq reads one: to the next white space, a backslash taking
  // the character after it.
  std::size_t WordEnd(std::size_t at) const {
    while (at < n_ && !IsSpace(t_[at])) at += t_[at] == '\\' ? std::min<std::size_t>(2, n_ - at) : 1;
    return at;
  }

  // The value's end without the quote that closes what the whole pair sits in: in
  // `at "host=h password=pw": refused` the value is pw, and `":` is the message's. Only one
  // quote, and only one that was opened before the key, is ever taken for a closing one.
  std::size_t WithoutCloser(std::size_t value, std::size_t end) const {
    std::size_t at = end;
    while (at > value && std::string_view(":,.;)]}").find(t_[at - 1]) != std::string_view::npos) at--;
    if (at > value && ((t_[at - 1] == '\'' && single_quotes_ % 2 == 1) || (t_[at - 1] == '"' && double_quotes_ % 2 == 1))) {
      return at - 1;
    }
    return end;
  }

  // `name =` at `at`, as a connection string's next pair would start.
  bool PairStarts(std::size_t at) const {
    if (at >= n_ || !IsIdentStart(t_[at])) return false;
    while (at < n_ && IsNameChar(t_[at])) at++;
    at = Spaces(at);
    return at < n_ && t_[at] == '=';
  }

  std::size_t ValueEnd(std::size_t value, Kind kind) const {
    if (value >= n_) return n_;
    if (t_.substr(value).starts_with(kMask)) {
      // Masked already: the mask is the whole value.
      while (value < n_ && t_[value] == '*') value++;
      return value;
    }
    if (kind == Kind::kQuery) {
      std::size_t at = value;
      while (at < n_ && t_[at] != '&' && t_[at] != '#' && !IsSpace(t_[at])) at++;
      return WithoutCloser(value, at);
    }
    if (kind == Kind::kHeader) {
      if (t_[value] == '\'' || t_[value] == '"') return QuotedEnd(value, t_[value]);
      return WithoutCloser(value, LineEnd(value));
    }
    std::size_t quoted = value;
    if (value + 1 < n_ && (t_[value] == 'e' || t_[value] == 'E') && t_[value + 1] == '\'') quoted++;
    if (t_[quoted] == '\'') return QuotedEnd(quoted, '\'');
    if (kind == Kind::kArgument) {
      if (t_[value] == '"') return QuotedEnd(value, '"');
      // A value in text that reads like SQL or JSON: to what ends an argument there.
      std::size_t at = value;
      while (at < n_ && !IsSpace(t_[at]) && std::string_view(",)}];'\"").find(t_[at]) == std::string_view::npos) at++;
      return at;
    }
    // A connection string's pair. Its grammars disagree about where an unquoted value ends
    // (libpq: at white space, so ';' and quotes are the password's; ODBC, ADO and Azure: at
    // ';', so spaces are), and about "..": the value ends where the later of them puts it.
    const std::size_t word = WordEnd(value);
    if (t_[value] == '"' || t_[value] == '{') {
      const std::size_t closed = QuotedEnd(value, t_[value], /*doubling=*/last_solid_before_key_ == ';' || t_[value] == '{');
      if (closed == n_ && (n_ == 0 || t_[n_ - 1] != (t_[value] == '{' ? '}' : '"'))) return n_;  // never closed
      if (closed >= word || last_solid_before_key_ == ';') return closed;
    }
    if (last_solid_before_key_ == ';') {
      // One pair of a ;-separated list: to the next ';', or to the end of the line (to the
      // last quote on it, when the list sits in quotes).
      const std::size_t line = LineEnd(value);
      const std::size_t semicolon = t_.substr(0, line).find(';', value);
      if (semicolon != std::string_view::npos) return semicolon;
      const char closer = double_quotes_ % 2 == 1 ? '"' : single_quotes_ % 2 == 1 ? '\'' : '\0';
      const std::size_t last = closer == '\0' ? std::string_view::npos : t_.substr(0, line).rfind(closer);
      return last != std::string_view::npos && last >= value ? last : line;
    }
    const std::size_t end = WithoutCloser(value, word);
    // What follows decides between the grammars: another `name=` pair, or nothing, is
    // libpq's. Anything else before a `;name=` is the rest of a value with spaces in it.
    const std::size_t next = Spaces(word);
    if (next >= n_ || t_[next] == '\n' || PairStarts(next)) return end;
    const std::size_t limit = std::min({n_, LineEnd(word), word + kLookAhead});
    for (std::size_t at = word; at < limit; at++) {
      if (t_[at] == ';' && PairStarts(Spaces(at + 1))) return at;
    }
    return end;
  }

  // scheme://userinfo@host at the "://" at `at`. Returns where reading goes on.
  std::size_t Url(std::size_t at) {
    std::size_t scheme_start = at;
    while (scheme_start > 0 && IsSchemeChar(t_[scheme_start - 1])) scheme_start--;
    const std::string scheme = Lower(t_.substr(scheme_start, at - scheme_start));
    const std::size_t start = at + 3;
    Plain("://");
    // The URL runs to the next white space; its last '@' ends the userinfo.
    if (start < segment_start_ || start >= segment_end_) {
      segment_start_ = start;
      segment_end_ = start;
      while (segment_end_ < n_ && !IsSpace(t_[segment_end_])) segment_end_++;
      const auto last = t_.substr(0, segment_end_).rfind('@');
      segment_last_at_ = last != std::string_view::npos && last >= start ? last : std::string_view::npos;
    }
    std::size_t end = segment_end_;
    std::size_t last_at = segment_last_at_ != std::string_view::npos && segment_last_at_ >= start ? segment_last_at_
                                                                                                 : std::string_view::npos;
    // A password with a space in it puts the last '@' past the white space. Where the URL's
    // end is known it is looked for up to there: the end of a text that is this one URL, or
    // the quote that closes the URL in a message that quotes it (never past another URL).
    std::size_t reach = end;
    if (whole_ && NextUrl(start) == std::string_view::npos) {
      reach = n_;
    } else if (double_quotes_ % 2 == 1 || single_quotes_ % 2 == 1) {
      const std::size_t limit = std::min({LineEnd(end), end + 2 * kLookAhead, NextUrl(start)});
      const auto closing = t_.substr(0, limit).rfind(double_quotes_ % 2 == 1 ? '"' : '\'');
      if (closing != std::string_view::npos && closing > end) reach = closing;
    }
    if (reach > end) {
      const auto far = t_.substr(0, reach).rfind('@');
      if (far != std::string_view::npos && far >= end) last_at = far;
    }
    // An '@' with no host after it (a letter, a digit or an IPv6 '[') is the password's own.
    if (last_at != std::string_view::npos && (last_at + 1 >= n_ || !(IsAlnum(t_[last_at + 1]) || t_[last_at + 1] == '['))) {
      end = last_at + 1;
      while (end < n_ && !IsSpace(t_[end])) end++;
      last_at = std::string_view::npos;
    }
    std::size_t first = start;  // the first of : / ? # @
    while (first < end && std::string_view(":/?#@").find(t_[first]) == std::string_view::npos) first++;
    const bool http = Among(kHttpSchemes, scheme);
    if (first < end && t_[first] == ':') {
      if (last_at == std::string_view::npos) {
        // user:password with a space in the password: the '@' is further on, before any
        // other URL starts.
        const std::size_t limit = std::min({n_, LineEnd(end), end + kLookAhead});
        for (std::size_t i = end; i < limit; i++) {
          if (t_[i] == ':' && t_.substr(i, 3) == "://") break;
          if (t_[i] != '@') continue;
          while (i < limit && !IsSpace(t_[i])) {
            if (t_[i] == '@') last_at = i;
            i++;
          }
          break;
        }
      }
      if (last_at != std::string_view::npos) {
        if (http) {
          Masked(start, last_at, kMask, /*joins=*/false);
          found_.Secret(t_.substr(start, first - start));  // the user, which may be the token
          found_.Secret(t_.substr(first + 1, last_at - first - 1));
          found_.Secret(PercentDecoded(t_.substr(first + 1, last_at - first - 1)));
        } else {
          Plain(t_.substr(start, first + 1 - start));
          Masked(first + 1, last_at, kMask, /*joins=*/false);
        }
        return last_at;
      }
      if (first + 1 >= n_) {
        // `scheme://user:` and then the end: the password is what the text is joined with.
        Plain(t_.substr(start, first + 1 - start));
        out_ += kMask;
        open_ = true;
        found_.credentials = true;
        return n_;
      }
      return start;
    }
    if (http && first < end && t_[first] == '@') {
      // A token as the user: https://TOKEN@host. (The last '@' before the path.)
      std::size_t path = first;
      while (path < end && std::string_view("/?#").find(t_[path]) == std::string_view::npos) path++;
      const std::size_t user_end = t_.substr(0, path).rfind('@');
      Masked(start, user_end, kMask, /*joins=*/false);
      return user_end;
    }
    return start;
  }

  // The next "://" at or after `from`, or npos. (Asked with rising positions: read once.)
  std::size_t NextUrl(std::size_t from) {
    if (!(next_url_from_ <= from && (next_url_ == std::string_view::npos || from <= next_url_))) {
      next_url_from_ = from;
      next_url_ = t_.find("://", from);
    }
    return next_url_;
  }

  static constexpr std::size_t kLookAhead = 512;

  const std::string_view t_;
  const std::size_t n_;
  Found& found_;
  const bool whole_;
  std::size_t next_url_from_ = 1;
  std::size_t next_url_ = 0;
  std::string out_;
  bool open_ = false;
  // On the line being read, before the reading position: how many of each quote, and the
  // last character that is not white space (what a key follows).
  unsigned single_quotes_ = 0;
  unsigned double_quotes_ = 0;
  char last_solid_ = 0;
  char last_solid_before_key_ = 0;
  std::size_t segment_start_ = 1;
  std::size_t segment_end_ = 0;
  std::size_t segment_last_at_ = std::string_view::npos;

};

std::string MaskText(std::string_view text, Found& found, bool* open = nullptr, bool whole = false) {
  return TextMasker(text, found, whole).Run(open);
}

}  // namespace

bool IsCredentialName(std::string_view name) { return EndsInCredentialName(Normal(name)); }

// libpq, when it cannot parse a connection string, quotes the piece it stopped at, which may
// be the password and stands next to no key: `Failed to parse connection string: "invalid
// percent-encoded token: "<piece>""`. What that message quotes is masked, the message kept.
static std::string WithoutUnparsedPieces(std::string text) {
  static constexpr std::string_view kPhrase = "Failed to parse connection string";
  for (std::size_t at = 0; (at = text.find(kPhrase, at)) != std::string::npos;) {
    at += kPhrase.size();
    const std::size_t outer = text.find('"', at);
    const std::size_t inner = outer == std::string::npos ? outer : text.find('"', outer + 1);
    const std::size_t last = text.rfind('"');  // closes the message; the one before it closes the piece
    const std::size_t close = last == std::string::npos || last == 0 ? std::string::npos : text.rfind('"', last - 1);
    if (inner == std::string::npos || close == std::string::npos || close <= inner) continue;
    text.replace(inner + 1, close - inner - 1, kMask);
  }
  return text;
}

std::string RedactCredentials(std::string text) {
  text = WithoutUnparsedPieces(std::move(text));
  Found found;
  std::string masked = MaskText(text, found);
  // What was masked is replaced wherever else the text has it (an error that quotes a
  // connection string, and then one piece of its password).
  return found.secrets.empty() ? masked : RedactSecrets(std::move(masked), found.secrets);
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

std::vector<std::string> CredentialsIn(std::string_view value) {
  Found found;
  MaskText(value, found);
  return std::move(found.secrets);
}

std::string RedactSecrets(std::string text, const std::vector<std::string>& secrets) {
  if (secrets.empty() || text.empty()) return text;
  std::vector<std::string> wanted;
  for (const auto& secret : secrets) {
    if (secret.empty() || AllStars(secret)) continue;
    wanted.push_back(Lower(secret));
    // And its pieces: a reader that splits the value where it has a delimiter (libpq, given
    // a URL whose password has an '@' or a ':' in it) quotes one piece of it in its error.
    static constexpr std::string_view kDelimiters = " \t\r\n:/@?#;&='\"%\\()[]{},";
    if (secret.find_first_of(kDelimiters) == std::string::npos) continue;
    for (std::size_t at = 0; at < secret.size();) {
      const std::size_t end = std::min(secret.find_first_of(kDelimiters, at), secret.size());
      if (end - at >= 3) wanted.push_back(Lower(std::string_view(secret).substr(at, end - at)));
      at = end + 1;
    }
  }
  // The longest first, so a value that begins another is not what gets replaced.
  std::sort(wanted.begin(), wanted.end(), [](const std::string& a, const std::string& b) {
    return a.size() != b.size() ? a.size() > b.size() : a < b;
  });
  wanted.erase(std::unique(wanted.begin(), wanted.end()), wanted.end());
  std::string lowered = Lower(text);
  for (const auto& secret : wanted) {
    std::size_t from = 0;
    for (std::size_t at; (at = lowered.find(secret, from)) != std::string::npos;) {
      const std::size_t end = at + secret.size();
      const bool alone = (at == 0 || !IsAlnum(lowered[at - 1])) && (end >= lowered.size() || !IsAlnum(lowered[end]));
      if (secret.size() < 4 && !alone) {
        from = at + 1;
        continue;
      }
      text.replace(at, secret.size(), kMask);
      lowered.replace(at, secret.size(), kMask);
      from = at + kMask.size();
    }
  }
  return text;
}

std::string RedactError(std::string_view error, const MaskedStatement& statement, const std::vector<std::string>& bound) {
  if (statement.unreadable) {
    return "[REDACTED: error of a statement that could not be read: " + *statement.unreadable + "]";
  }
  if (statement.computed) return "[REDACTED: error of a statement that computes a credential value]";
  std::string text(error);
  // DuckDB's excerpt of the statement, which may end in the middle of a value.
  if (auto at = text.find("\nLINE "); at != std::string::npos) {
    text.resize(at);
    while (!text.empty() && IsSpace(text.back())) text.pop_back();  // (the blank line before the excerpt)
  }
  text = RedactSecrets(std::move(text), statement.secrets);
  text = RedactSecrets(std::move(text), bound);
  return RedactCredentials(std::move(text));
}

// ---- A statement, as DuckDB's tokens ------------------------------------------------

namespace {

using Kind = SqlTokenKind;

// What a string literal holds, as DuckDB reads it (StringLiteralMatcher::StripStringLiteral,
// then StringLiteralParseResult::ToExpression for E'..').
std::string LiteralBody(std::string_view token) {
  if (token.starts_with('$')) {
    const std::size_t tag = token.find('$', 1);
    if (tag == std::string_view::npos || token.size() < 2 * (tag + 1)) return std::string(token);
    return std::string(token.substr(tag + 1, token.size() - 2 * (tag + 1)));
  }
  const std::size_t open = token.find('\'');
  if (open == std::string_view::npos || token.size() < open + 2) return std::string(token);
  const bool escapes = open == 1 && (token[0] == 'e' || token[0] == 'E');
  std::string plain;
  const std::string_view inner = token.substr(open + 1, token.size() - open - 2);
  for (std::size_t i = 0; i < inner.size(); i++) {
    plain += inner[i];
    if (inner[i] == '\'' && i + 1 < inner.size() && inner[i + 1] == '\'') i++;
  }
  if (!escapes) return plain;
  std::string out;
  for (std::size_t i = 0; i < plain.size(); i++) {
    if (plain[i] != '\\' || i + 1 >= plain.size()) {
      out += plain[i];
      continue;
    }
    const char c = plain[++i];
    if (c >= '0' && c <= '7') {
      std::size_t end = i + 1;
      while (end < plain.size() && end - i < 3 && plain[end] >= '0' && plain[end] <= '7') end++;
      out += static_cast<char>(std::strtoul(plain.substr(i, end - i).c_str(), nullptr, 8));
      i = end - 1;
    } else if (c == 'x') {
      std::size_t end = i + 1;
      while (end < plain.size() && end - i - 1 < 2 && std::isxdigit(static_cast<unsigned char>(plain[end]))) end++;
      if (end > i + 1) {
        out += static_cast<char>(std::strtoul(plain.substr(i + 1, end - i - 1).c_str(), nullptr, 16));
        i = end - 1;
      } else {
        out += 'x';
      }
    } else {
      out += c == 'b' ? '\b' : c == 'f' ? '\f' : c == 'n' ? '\n' : c == 't' ? '\t' : c == 'r' ? '\r' : c;
    }
  }
  return out;
}

std::string NameBody(std::string_view token) {
  if (token.size() < 2) return std::string(token);
  const char quote = token.front();
  std::string out;
  for (std::size_t i = 1; i + 1 < token.size(); i++) {
    out += token[i];
    if (token[i] == quote && i + 2 < token.size() && token[i + 1] == quote) i++;
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

bool IsKeyFunction(std::string_view lower_name) { return lower_name == "add_parquet_key"; }

// Words that end the operand of a comparison (x = 'v' AND ..., ... WHERE ...).
constexpr std::string_view kNextClause[] = {
    "and",    "or",        "where",     "from", "group", "order", "having", "limit",  "offset",  "union",
    "except", "intersect", "returning", "on",   "join",  "using", "as",     "window", "qualify", "then",
    "else",   "when",      "end",       "escape"};

// Words a '(' can follow without being a function's argument list.
constexpr std::string_view kNotFunctions[] = {
    "and",    "or",    "not",  "where",   "on",     "when",   "then",      "else",   "in",     "select", "from",
    "having", "by",    "values", "as",    "using",  "exists", "any",       "all",    "some",   "case",   "between",
    "like",   "ilike", "join", "union",   "except", "intersect", "over",   "filter", "within", "limit",  "offset",
    "set",    "returning", "qualify", "with", "table", "into", "distinct", "is",     "lateral", "recursive"};

// Text worth a second reading as SQL: it names something credential-like and has a quote
// or a space for a value to stand in.
bool MayHoldSql(std::string_view text) {
  if (text.size() < 8 || text.find_first_of("'\"$ ") == std::string_view::npos) return false;
  const std::string lower = Lower(text);
  if (lower.find("identified") != std::string::npos) return true;
  return std::any_of(std::begin(kCredentialNames), std::end(kCredentialNames),
                     [&](std::string_view word) { return lower.find(word) != std::string::npos; });
}

// Text DuckDB cannot tokenize that names a credential and then opens a quote: a value may
// follow the name in a grammar that is not DuckDB's (a backslash-escaped quote in MySQL's).
bool NamesAQuotedCredential(std::string_view text) {
  const std::string lower = Lower(text);
  auto before_a_quote = [&](std::string_view word) {
    for (std::size_t at = 0; (at = lower.find(word, at)) != std::string::npos; at++) {
      if (lower.find_first_of("'\"$", at + word.size()) != std::string::npos) return true;
    }
    return false;
  };
  return before_a_quote("identified") ||
         std::any_of(std::begin(kCredentialNames), std::end(kCredentialNames), before_a_quote);
}

constexpr int kDeepestNesting = 4;  // SQL in a literal in SQL in a literal ...

class SqlMasker {
 public:
  SqlMasker(const SqlTokens& tokens, int depth) : t_(tokens), n_(tokens.size()), depth_(depth) {}

  // Reads every statement. `out`: the whole text's result, and each statement's when there
  // are several (`limit` bounds each text). Nested (depth > 0): only the whole, unbounded.
  void Run(MaskedSql& out, std::size_t limit) {
    struct Span {
      std::size_t begin, end;      // tokens
      std::size_t secrets, parameters;  // where its findings start in found_
      bool credentials, computed;
    };
    std::vector<Span> spans;
    for (std::size_t begin = 0; begin < n_;) {
      std::size_t end = begin;
      while (end < n_ && t_.kind(end) != Kind::kTerminator) end++;
      if (Sig(begin, end) < end) {
        Found before = std::move(found_);
        found_ = Found{};
        Statement(begin, end);
        spans.push_back({begin, end, before.secrets.size(), before.parameters.size(), found_.credentials, found_.computed});
        // Fold this statement's findings into the request's.
        for (auto& secret : found_.secrets) before.Secret(secret);
        for (auto& parameter : found_.parameters) before.parameters.push_back(std::move(parameter));
        before.credentials = before.credentials || found_.credentials;
        before.computed = before.computed || found_.computed;
        found_ = std::move(before);
      } else {
        Trivia(begin, end);
      }
      begin = end + 1;
    }
    Settle();
    out.text = Text(0, n_, limit);
    out.secrets = found_.secrets;
    out.parameters = found_.parameters;
    out.credentials = found_.credentials;
    out.computed = found_.computed;
    if (depth_ > 0 || spans.size() < 2) return;
    for (std::size_t i = 0; i < spans.size(); i++) {
      const Span& span = spans[i];
      MaskedStatement statement;
      statement.text = Text(Sig(span.begin, span.end), span.end, limit);
      const std::size_t secrets_end = i + 1 < spans.size() ? spans[i + 1].secrets : found_.secrets.size();
      const std::size_t parameters_end = i + 1 < spans.size() ? spans[i + 1].parameters : found_.parameters.size();
      for (std::size_t s = span.secrets; s < secrets_end && s < found_.secrets.size(); s++) {
        statement.secrets.push_back(found_.secrets[s]);
      }
      for (std::size_t p = span.parameters; p < parameters_end; p++) statement.parameters.push_back(found_.parameters[p]);
      statement.credentials = span.credentials;
      // (Past the cap on listed values, which statement the rest belong to is not kept.)
      statement.computed = span.computed || found_.secrets.size() >= Found::kMostSecrets;
      out.statements.push_back(std::move(statement));
    }
  }

  Found& found() { return found_; }
  bool changed() const { return !edits_.empty(); }

 private:
  // Tokens [first, last) are written as `text`.
  struct Edit {
    std::size_t first, last;
    std::string text;
  };

  // ---- reading tokens ----

  std::string_view text(std::size_t i) const { return t_[i].text; }
  bool trivia(std::size_t i) const { return t_.kind(i) == Kind::kSpace || t_.kind(i) == Kind::kComment; }
  // The first token at or after `i` that is neither white space nor a comment, or `end`.
  std::size_t Sig(std::size_t i, std::size_t end) const {
    while (i < end && trivia(i)) i++;
    return std::min(i, end);
  }
  bool IsOp(std::size_t i, std::string_view op) const { return t_.kind(i) == Kind::kOperator && text(i) == op; }
  bool IsWord(std::size_t i, std::string_view lower) const {
    if (t_.kind(i) != Kind::kWord || text(i).size() != lower.size()) return false;
    return Lower(text(i)) == lower;
  }
  // A mask this code wrote, read again: ***, or an operator it was written against (=***).
  bool IsMask(std::size_t i) const { return t_.kind(i) == Kind::kOperator && text(i).ends_with(kMask); }
  bool Opens(std::size_t i) const { return IsOp(i, "(") || IsOp(i, "[") || IsOp(i, "{"); }
  bool Closes(std::size_t i) const { return IsOp(i, ")") || IsOp(i, "]") || IsOp(i, "}"); }
  // The token that closes the bracket opened at `open`, or `end`.
  std::size_t Matching(std::size_t open, std::size_t end) const {
    std::size_t depth = 0;
    for (std::size_t i = open; i < end; i++) {
      if (Opens(i)) depth++;
      else if (Closes(i) && --depth == 0) return i;
    }
    return end;
  }
  // The name a token is, when it is one (an unquoted word or a quoted name), lower case.
  std::optional<std::string> NameAt(std::size_t i) const {
    if (t_.kind(i) == Kind::kWord) return Lower(text(i));
    if (t_.kind(i) == Kind::kQuotedName) return Lower(NameBody(text(i)));
    return std::nullopt;
  }

  // ---- writing ----

  void Replace(std::size_t first, std::size_t last, std::string replacement) {
    if (first < last) edits_.push_back({first, last, std::move(replacement)});
  }

  // The edits in order and disjoint: where two overlap, the one that starts first (and of
  // two that start together, the longer) stands, so a masked value covers what is inside it.
  void Settle() {
    std::stable_sort(edits_.begin(), edits_.end(), [](const Edit& a, const Edit& b) {
      return a.first != b.first ? a.first < b.first : a.last > b.last;
    });
    std::vector<Edit> kept;
    for (auto& edit : edits_) {
      if (kept.empty() || edit.first >= kept.back().last) kept.push_back(std::move(edit));
    }
    edits_ = std::move(kept);
  }

  std::string Text(std::size_t begin, std::size_t end, std::size_t limit) const {
    std::string out;
    auto edit = std::lower_bound(edits_.begin(), edits_.end(), begin,
                                 [](const Edit& e, std::size_t at) { return e.first < at; });
    for (std::size_t i = begin; i < end;) {
      std::string_view piece;
      std::size_t next = i + 1;
      if (edit != edits_.end() && edit->first == i) {
        piece = edit->text;
        next = edit->last;
        ++edit;
      } else {
        piece = text(i);
      }
      if (out.size() + piece.size() > limit) {
        out += kCut;
        break;
      }
      out += piece;
      i = next;
    }
    return out;
  }

  // ---- values ----

  enum class Shape {
    kArgument,  // after =>, := or a key's colon: to the next , ) or ;
    kOperand,   // after = or a comparison, or an option list's NAME 'v': the same, or to the
                // word that starts the next clause
    kGroup,     // a function's whole argument list: to its )
  };

  // One past the last token of the value that starts at `start`. Unsure means further.
  std::size_t ValueEnd(std::size_t start, std::size_t end, Shape shape) const {
    std::size_t depth = 0, cases = 0, parts = 0, last = start;
    for (std::size_t i = start; i < end; i++) {
      if (trivia(i)) continue;
      if (depth == 0) {
        if (Closes(i) || t_.kind(i) == Kind::kTerminator || (IsOp(i, ",") && shape != Shape::kGroup)) break;
        if (shape == Shape::kOperand && t_.kind(i) == Kind::kWord) {
          const std::string word = Lower(text(i));
          if (word == "case") {
            cases++;
          } else if (word == "end" && cases > 0) {
            cases--;
          } else if (parts > 0 && cases == 0 && Among(kNextClause, word)) {
            break;
          }
        }
      }
      if (Opens(i)) depth++;
      if (Closes(i) && depth > 0) depth--;
      parts++;
      last = i + 1;
    }
    return last;
  }

  // A placeholder at `i` (?, $1, $name): one past it, or nothing.
  std::optional<std::size_t> Placeholder(std::size_t i, std::size_t end, MaskedParameter* parameter) const {
    if (IsOp(i, "?")) {
      if (parameter != nullptr) parameter->ordinal = OrdinalOf(i);
      return i + 1;
    }
    if (IsOp(i, "$") && i + 1 < end && (t_.kind(i + 1) == Kind::kNumber || t_.kind(i + 1) == Kind::kWord)) {
      if (parameter != nullptr) {
        if (t_.kind(i + 1) == Kind::kNumber) parameter->ordinal = std::strtoull(std::string(text(i + 1)).c_str(), nullptr, 10);
        else parameter->name = std::string(text(i + 1));
      }
      return i + 2;
    }
    return std::nullopt;
  }

  // Which `?` of its statement the one at `i` is, from 1. (Asked with rising positions:
  // the count goes on from the last one asked for.)
  std::size_t OrdinalOf(std::size_t i) const {
    if (i < counted_to_ || counted_from_ != statement_begin_) {
      counted_from_ = counted_to_ = statement_begin_;
      counted_ = 0;
    }
    for (; counted_to_ < i; counted_to_++) counted_ += IsOp(counted_to_, "?") ? 1 : 0;
    return counted_ + 1;
  }

  // Keeps what the value [first, last) is, for errors that quote it, and says whether it
  // is computed. Words are kept only when the value is nothing but a bare word.
  void Collect(std::size_t first, std::size_t last) {
    std::vector<std::string> strings;
    std::vector<std::string> words;
    bool computed = false;
    bool any = false;  // a token of the value was read
    for (std::size_t i = first; i < last; i++) {
      if (trivia(i)) continue;
      if (IsMask(i)) continue;  // masked before: nothing of the value is left in it
      const bool leading = !any;
      any = any || !(IsOp(i, "(") || IsOp(i, "-") || IsOp(i, "+"));
      MaskedParameter parameter;
      if (auto after = Placeholder(i, last, &parameter)) {
        found_.parameters.push_back(std::move(parameter));
        i = *after - 1;
        continue;
      }
      switch (t_.kind(i)) {
        case Kind::kString:
          strings.push_back(LiteralBody(text(i)));
          break;
        case Kind::kQuotedName:
          words.push_back(NameBody(text(i)));
          break;
        case Kind::kNumber:
          if (text(i) != ".") words.emplace_back(text(i));
          break;
        case Kind::kWord: {
          const std::size_t next = Sig(i + 1, last);
          const std::string word = Lower(text(i));
          if ((next < last && IsOp(next, "(")) || word == "select" || word == "case") computed = true;
          words.emplace_back(text(i));
          break;
        }
        default:
          if (!(IsOp(i, "||") || IsOp(i, "::") || IsOp(i, "(") || IsOp(i, ")") ||
                (leading && (IsOp(i, "-") || IsOp(i, "+"))))) {
            computed = true;
          }
      }
    }
    for (const auto& value : strings) found_.Secret(value);
    if (strings.size() > 1) {
      std::string joined;
      for (const auto& value : strings) joined += value;
      found_.Secret(joined);
    }
    if (strings.empty() && !computed) {
      for (const auto& word : words) found_.Secret(word);
    }
    // What a string literal of the value holds is read too (a connection string given to a
    // credential name): its parts are values of their own.
    for (const auto& value : strings) MaskText(value, found_, nullptr, /*whole=*/true);
    found_.computed = found_.computed || computed;
    found_.credentials = true;
  }

  // Masks the value [first, last): *** for it, unless it is a lone placeholder, which stays.
  void MaskValue(std::size_t first, std::size_t last) {
    if (first >= last) return;
    MaskedParameter parameter;
    if (auto after = Placeholder(first, last, &parameter); after && Sig(*after, last) >= last) {
      found_.parameters.push_back(std::move(parameter));
      found_.credentials = true;
      return;
    }
    if (IsMask(first) && Sig(first + 1, last) >= last) return;  // masked already
    Collect(first, last);
    Replace(first, last, std::string(kMask));
  }

  // Whether [first, last) is only a column's name (a, a.b, "a".b).
  bool IsColumn(std::size_t first, std::size_t last) const {
    bool any = false;
    for (std::size_t i = first; i < last; i++) {
      if (trivia(i)) continue;
      if (t_.kind(i) == Kind::kWord || t_.kind(i) == Kind::kQuotedName) any = true;
      else if (!(t_.kind(i) == Kind::kNumber && text(i) == ".")) return false;
    }
    return any;
  }

  // ---- what a token holds ----

  // Text inside a literal, a quoted name or a comment: read as text that is not a statement
  // and, when it may be one, as SQL of its own. Returns the masked text when it differs.
  std::optional<std::string> MaskBody(std::string_view body, bool sql, bool* open, bool whole = true) {
    if (body.size() < 4) return std::nullopt;
    Found inner;
    std::string masked = MaskText(body, inner, open, whole);
    bool changed = masked != body;
    if (sql && MayHoldSql(masked)) {
      const SqlTokens tokens = LexSql(masked);
      // SQL that is not DuckDB's to read: text DuckDB cannot tokenize, or with a backslash
      // before a quote (which ends a literal for DuckDB and is part of one for MySQL).
      const bool foreign = masked.find("\\'") != std::string::npos || masked.find("\\\"") != std::string::npos;
      if (tokens.unreadable() || foreign || depth_ >= kDeepestNesting) {
        if (NamesAQuotedCredential(masked)) {
          inner.Secret(body);
          inner.credentials = true;
          masked = std::string(kMask);
          changed = true;
        }
      } else {
        SqlMasker nested(tokens, depth_ + 1);
        MaskedSql result;
        nested.Run(result, std::string::npos);
        if (nested.changed()) {
          masked = std::move(result.text);
          changed = true;
        }
        for (auto& secret : nested.found().secrets) inner.Secret(secret);
        inner.credentials = inner.credentials || nested.found().credentials;
        inner.computed = inner.computed || nested.found().computed;
      }
    }
    for (auto& secret : inner.secrets) found_.Secret(secret);
    found_.credentials = found_.credentials || inner.credentials;
    found_.computed = found_.computed || inner.computed;
    if (!changed) return std::nullopt;
    return masked;
  }

  void Comment(std::size_t i) {
    const std::string_view comment = text(i);
    const bool line = comment.starts_with("--");
    std::size_t close = !line && comment.size() >= 4 && comment.ends_with("*/") ? 2 : 0;
    while (line && close < comment.size() - 2 && (comment[comment.size() - 1 - close] == '\n' ||
                                                  comment[comment.size() - 1 - close] == '\r')) {
      close++;  // (DuckDB's token for a line comment takes the line's end with it)
    }
    const std::string_view body = comment.substr(2, comment.size() - 2 - close);
    if (auto masked = MaskBody(body, /*sql=*/true, nullptr, /*whole=*/false)) {
      Replace(i, i + 1, std::string(comment.substr(0, 2)) + (*masked == kMask ? " *** " : *masked) +
                            std::string(comment.substr(comment.size() - close)));
    }
  }

  void Trivia(std::size_t begin, std::size_t end) {
    for (std::size_t i = begin; i < end; i++) {
      if (t_.kind(i) == Kind::kComment) Comment(i);
    }
  }

  // A string literal that is not a credential's value: what it holds may carry one.
  // Returns whether a value in it runs on into what the literal is joined with.
  bool Literal(std::size_t i) {
    bool open = false;
    if (auto masked = MaskBody(LiteralBody(text(i)), /*sql=*/true, &open)) Replace(i, i + 1, Quoted(*masked, '\''));
    return open;
  }

  // ---- statements ----

  enum class Whole { kNo, kSecret, kUser, kSetting };

  // A statement that is credential DDL as a whole, by its own first words.
  Whole WholeKind(std::size_t first, std::size_t end) const {
    std::vector<std::string> words;
    std::size_t at = first;
    for (; at < end && words.size() < 7; at = Sig(at + 1, end)) {
      if (t_.kind(at) != Kind::kWord) break;
      words.push_back(Lower(text(at)));
    }
    auto word = [&](std::size_t i) { return i < words.size() ? std::string_view(words[i]) : std::string_view(); };
    bool literal = false, bracket = false, name = false;
    for (std::size_t i = first; i < end; i++) {
      literal = literal || t_.kind(i) == Kind::kString;
      bracket = bracket || IsOp(i, "(");
      name = name || (t_.kind(i) == Kind::kWord && IsCredentialName(text(i)));
    }
    if (word(0) == "create") {
      std::size_t i = 1;
      if (word(i) == "or" && word(i + 1) == "replace") i += 2;
      if (word(i) == "persistent" || word(i) == "temporary" || word(i) == "temp") i++;
      // (Inside a literal or a comment, the words alone are no statement with a value.)
      if (word(i) == "secret" && (depth_ == 0 || literal || bracket)) return Whole::kSecret;
    }
    if ((word(0) == "create" || word(0) == "alter") && word(1) == "user" && (literal || name)) return Whole::kUser;
    if (word(0) == "set" || word(0) == "pragma") {
      std::size_t i = 1;
      if (word(i) == "global" || word(i) == "session" || word(i) == "local") i++;
      if (word(i) == "variable" || word(i).empty()) return Whole::kNo;
      // The setting's name: the word and what is joined to it by dots.
      std::size_t token = first;
      for (std::size_t skip = 0; skip < i; skip++) token = Sig(token + 1, end);
      std::string setting = Normal(text(token));
      for (std::size_t next = token + 1; next + 1 < end && t_.kind(next) == Kind::kNumber && text(next) == "." &&
                                         (t_.kind(next + 1) == Kind::kWord || t_.kind(next + 1) == Kind::kQuotedName);
           next += 2) {
        setting += "." + Normal(*NameAt(next + 1));
      }
      const std::size_t after = Sig(token + 1, end);
      if (after >= end) return Whole::kNo;  // no value is given
      const bool called = IsOp(after, "(");  // PRAGMA f(...): a function, masked by its arguments
      const bool credential = EndsInCredentialName(setting) ||
                              std::any_of(std::begin(kSettingWords), std::end(kSettingWords),
                                          [&](std::string_view w) { return setting.find(w) != std::string::npos; });
      if (credential && !called) return Whole::kSetting;
    }
    return Whole::kNo;
  }

  // Everything a wholly hidden statement gives a value to.
  void CollectWhole(std::size_t first, std::size_t end, Whole kind) {
    found_.credentials = true;
    if (kind == Whole::kSecret) {
      // ( NAME value, NAME value, ... ): every value but the secret's type.
      std::size_t open = first;
      while (open < end && !IsOp(open, "(")) open++;
      const std::size_t close = Matching(open, end);
      for (std::size_t item = Sig(open + 1, close); item < close;) {
        std::size_t stop = item, depth = 0;
        for (; stop < close; stop++) {
          if (Opens(stop)) depth++;
          else if (Closes(stop) && depth > 0) depth--;
          else if (depth == 0 && IsOp(stop, ",")) break;
        }
        const auto name = NameAt(item);
        const std::size_t value = Sig(item + 1, stop);
        if (value < stop && !(name && (*name == "type" || *name == "provider"))) {
          std::size_t last = value;
          for (std::size_t i = value; i < stop; i++) last = trivia(i) ? last : i + 1;
          Collect(value, last);
        }
        item = Sig(stop + 1, close);
      }
    }
    // And, whatever the statement: each literal, and the word a credential name is given.
    for (std::size_t i = first; i < end; i++) {
      if (t_.kind(i) == Kind::kString) {
        const std::string body = LiteralBody(text(i));
        found_.Secret(body);
        MaskText(body, found_);
      } else if (kind != Whole::kSecret && t_.kind(i) == Kind::kWord && IsCredentialName(text(i))) {
        std::size_t value = Sig(i + 1, end);
        if (value < end && (IsOp(value, "=") || IsWord(value, "to"))) value = Sig(value + 1, end);
        if (value < end && (t_.kind(value) == Kind::kWord || t_.kind(value) == Kind::kNumber ||
                            t_.kind(value) == Kind::kQuotedName)) {
          const std::size_t call = Sig(value + 1, end);
          if (call < end && IsOp(call, "(")) found_.computed = true;
          found_.Secret(t_.kind(value) == Kind::kQuotedName ? NameBody(text(value)) : std::string(text(value)));
        }
      } else if (IsOp(i, "||")) {
        found_.computed = true;
      }
    }
    if (kind == Whole::kSetting) {
      // SET name = value / TO value: the value, whatever it is.
      for (std::size_t i = first; i < end; i++) {
        if (!(IsOp(i, "=") || IsWord(i, "to"))) continue;
        const std::size_t value = Sig(i + 1, end);
        std::size_t last = value;
        for (std::size_t j = value; j < end; j++) last = trivia(j) ? last : j + 1;
        if (value < last) Collect(value, last);
        break;
      }
    }
  }

  // INSERT INTO t (a, password, c) VALUES (..), (..): the values given to the credential
  // columns, row by row. Returns ranges of tokens, in order.
  std::vector<std::pair<std::size_t, std::size_t>> InsertedCredentials(std::size_t first, std::size_t end) const {
    std::vector<std::pair<std::size_t, std::size_t>> values;
    if (!IsWord(first, "insert")) return values;
    std::size_t at = Sig(first + 1, end);
    while (at < end && !IsWord(at, "into")) at = Sig(at + 1, end);  // OR REPLACE, OR IGNORE
    // The table's name, then the column list.
    for (at = Sig(at + 1, end); at < end && !IsOp(at, "("); at = Sig(at + 1, end)) {
      if (IsWord(at, "values") || IsWord(at, "select") || IsWord(at, "by") || IsWord(at, "default")) return values;
    }
    const std::size_t close = Matching(at, end);
    std::vector<std::size_t> columns;
    std::size_t column = 0;
    for (std::size_t i = Sig(at + 1, close); i < close; i = Sig(i + 1, close)) {
      if (IsOp(i, ",")) {
        column++;
      } else if (const auto name = NameAt(i); name && IsCredentialName(*name)) {
        if (columns.empty() || columns.back() != column) columns.push_back(column);
      }
    }
    if (columns.empty()) return values;
    std::size_t row = Sig(close + 1, end);
    if (row >= end || !IsWord(row, "values")) return values;
    for (row = Sig(row + 1, end); row < end && IsOp(row, "("); ) {
      const std::size_t row_close = Matching(row, end);
      std::size_t index = 0, start = row + 1, depth = 0;
      for (std::size_t i = row + 1; i <= row_close && i < end; i++) {
        if (i < row_close && Opens(i)) depth++;
        else if (i < row_close && Closes(i) && depth > 0) depth--;
        if (i == row_close || (depth == 0 && IsOp(i, ","))) {
          if (std::binary_search(columns.begin(), columns.end(), index)) {
            const std::size_t value = Sig(start, i);
            std::size_t last = value;
            for (std::size_t j = value; j < i; j++) last = trivia(j) ? last : j + 1;
            if (value < last) values.emplace_back(value, last);
          }
          index++;
          start = i + 1;
        }
      }
      row = Sig(row_close + 1, end);
      if (row >= end || !IsOp(row, ",")) break;
      row = Sig(row + 1, end);
    }
    return values;
  }

  // MAP([k1, k2], [v1, v2]) at the word `map`: the values under credential keys.
  void MapLists(std::size_t map, std::size_t end, std::vector<std::pair<std::size_t, std::size_t>>& ahead) const {
    const std::size_t open = Sig(map + 1, end);
    if (open >= end || !IsOp(open, "(")) return;
    const std::size_t keys = Sig(open + 1, end);
    if (keys >= end || !IsOp(keys, "[")) return;
    const std::size_t keys_close = Matching(keys, end);
    std::vector<std::size_t> wanted;
    std::size_t index = 0;
    for (std::size_t i = Sig(keys + 1, keys_close); i < keys_close; i = Sig(i + 1, keys_close)) {
      if (IsOp(i, ",")) index++;
      else if (t_.kind(i) == Kind::kString && IsCredentialName(LiteralBody(text(i)))) wanted.push_back(index);
    }
    std::size_t values = Sig(keys_close + 1, end);
    if (wanted.empty() || values >= end || !IsOp(values, ",")) return;
    values = Sig(values + 1, end);
    if (values >= end || !IsOp(values, "[")) return;
    const std::size_t values_close = Matching(values, end);
    std::size_t start = values + 1, depth = 0;
    index = 0;
    for (std::size_t i = values + 1; i <= values_close && i < end; i++) {
      if (i < values_close && Opens(i)) depth++;
      else if (i < values_close && Closes(i) && depth > 0) depth--;
      if (i == values_close || (depth == 0 && IsOp(i, ","))) {
        if (std::binary_search(wanted.begin(), wanted.end(), index)) {
          const std::size_t value = Sig(start, i);
          std::size_t last = value;
          for (std::size_t j = value; j < i; j++) last = trivia(j) ? last : j + 1;
          if (value < last) ahead.emplace_back(value, last);
        }
        index++;
        start = i + 1;
      }
    }
  }

  void Statement(std::size_t begin, std::size_t end) {
    statement_begin_ = begin;
    const std::size_t first = Sig(begin, end);
    Trivia(begin, first);
    if (const Whole whole = WholeKind(first, end); whole != Whole::kNo) {
      CollectWhole(first, end, whole);
      std::size_t last = first;
      for (std::size_t i = first; i < end; i++) last = trivia(i) ? last : i + 1;
      Replace(first, last, whole == Whole::kSecret ? "[REDACTED: secret DDL]"
                           : whole == Whole::kUser ? "[REDACTED: user DDL]" : "[REDACTED: a credential setting]");
      return;
    }
    Walk(first, end);
  }

  // An ordinary statement, token by token.
  void Walk(const std::size_t first, const std::size_t end) {
    const bool attach = IsWord(first, "attach");
    const bool set = IsWord(first, "set") || IsWord(first, "reset") || IsWord(first, "pragma");
    // Values found ahead of the reading position (an INSERT's rows, a MAP's second list).
    std::vector<std::pair<std::size_t, std::size_t>> ahead = InsertedCredentials(first, end);
    std::size_t ahead_at = 0;
    std::vector<bool> calls;  // for each open bracket: whether it is a function's argument list
    std::vector<bool> named;  // ... and whether a credential name stands in it: lower(password), CAST(password AS TEXT)
    enum class After { kNothing, kName, kWrappedName, kLiteralKey, kKeyFunction };
    After after = After::kNothing;
    bool name_is_argument = false;  // the credential name starts a function's argument
    std::size_t previous = end;     // the significant token before this one
    std::size_t chain_before = end; // the significant token before the name chain (a.b.c) being read

    // The value that starts at `value`, for the credential name just read.
    auto operand = [&](std::size_t value, Shape shape, bool column_is_no_value) {
      if (value < end && IsMask(value)) {
        // Masked already (the text was read before, or its key=value reading wrote the mask).
        // The mask is the value when what follows ends a value; when it does not (`*** ||
        // 'rest'`), the value goes on and all of it is masked.
        const std::size_t next = Sig(value + 1, end);
        if (next >= end || IsOp(next, ",") || Closes(next) || t_.kind(next) == Kind::kTerminator ||
            (t_.kind(next) == Kind::kWord && Among(kNextClause, Lower(text(next))))) {
          return value + 1;
        }
      }
      const std::size_t last = ValueEnd(value, end, shape);
      if (!(column_is_no_value && IsColumn(value, last))) MaskValue(value, last);
      return std::max(last, value);
    };
    // After the name and the token at `i`: where reading goes on when `i` gives the name a
    // value (which is masked), or nothing.
    std::function<std::optional<std::size_t>(std::size_t, After, int)> value_after =
        [&](std::size_t i, After was, int casts) -> std::optional<std::size_t> {
      const bool name = was == After::kName || was == After::kWrappedName;
      const std::size_t next = Sig(i + 1, end);
      if (t_.kind(i) == Kind::kOperator) {
        const std::string_view op = text(i);
        if (op.size() > kMask.size() && op.ends_with(kMask)) return i + 1;  // name=***: masked already
        if (was == After::kKeyFunction) {
          if (op != "(") return std::nullopt;
          const std::size_t close = Matching(i, end);
          std::size_t last = next;
          for (std::size_t j = next; j < close; j++) last = trivia(j) ? last : j + 1;
          MaskValue(next, last);
          return close;
        }
        if (op == "=>" || op == ":=" || op == ":") return operand(next, Shape::kArgument, false);
        if (!name) {
          // 'key', 'value': a pair of literals, as an argument list or a row gives them.
          if (op == "," && next < end && t_.kind(next) == Kind::kString) {
            const std::size_t then = Sig(next + 1, end);
            if (then >= end || IsOp(then, ",") || Closes(then) || t_.kind(then) == Kind::kTerminator) {
              MaskValue(next, next + 1);
              return next + 1;
            }
          }
          return std::nullopt;
        }
        if (op == "::" && casts < 4) {
          // name::TYPE = value: past the type (and its (n)), the same question again.
          std::size_t type_end = Sig(next + 1, end);
          if (type_end < end && IsOp(type_end, "(")) type_end = Sig(Matching(type_end, end) + 1, end);
          if (type_end >= end) return std::nullopt;
          return value_after(type_end, After::kWrappedName, casts + 1);
        }
        const bool comparison = op.starts_with('=') || op.starts_with('<') || op.starts_with('>') ||
                                op.starts_with("!=") || op.starts_with("~") || op.starts_with("!~");
        if (!comparison) return std::nullopt;
        // `name = other_column` compares two columns: nothing to hide. In a function's
        // argument list (f(token = x)), in SET and in ATTACH a bare word is a value.
        return operand(next, Shape::kOperand, !(name_is_argument || set || attach));
      }
      if (was != After::kName) {
        if (was == After::kWrappedName && t_.kind(i) == Kind::kWord) {
          // lower(name) IN (...), name::TEXT LIKE '..'
        } else {
          return std::nullopt;
        }
      }
      if (t_.kind(i) == Kind::kString && was == After::kName) return operand(i, Shape::kOperand, false);  // NAME 'value'
      if (attach && was == After::kName &&
          (t_.kind(i) == Kind::kWord || t_.kind(i) == Kind::kQuotedName || t_.kind(i) == Kind::kNumber)) {
        return operand(i, Shape::kOperand, false);  // an ATTACH option's bare value: (TOKEN abc)
      }
      if (t_.kind(i) != Kind::kWord || next >= end) return std::nullopt;
      std::string word = Lower(text(i));
      std::size_t at = i, value = next;
      if (word == "not" && t_.kind(next) == Kind::kWord) {
        at = next;
        word = Lower(text(next));
        value = Sig(next + 1, end);
        if (value >= end) return std::nullopt;
      }
      if (word == "in") {
        if (!IsOp(value, "(")) return operand(value, Shape::kOperand, false);
        const std::size_t close = Matching(value, end);
        const std::size_t inner = Sig(value + 1, close);
        std::size_t last = inner;
        for (std::size_t j = inner; j < close; j++) last = trivia(j) ? last : j + 1;
        MaskValue(inner, last);
        return close;
      }
      if (word == "like" || word == "ilike" || word == "glob" || word == "regexp" || word == "rlike") {
        return operand(value, Shape::kOperand, false);
      }
      if (word == "similar" && IsWord(value, "to")) return operand(Sig(value + 1, end), Shape::kOperand, false);
      if (word == "between") {
        const std::size_t low = operand(value, Shape::kOperand, false);
        const std::size_t conjunction = Sig(low, end);
        if (conjunction < end && IsWord(conjunction, "and")) return operand(Sig(conjunction + 1, end), Shape::kOperand, false);
        return low;
      }
      if (word == "is" && at == i) {
        std::size_t distinct = value;
        if (IsWord(distinct, "not")) distinct = Sig(distinct + 1, end);
        if (distinct < end && IsWord(distinct, "distinct")) {
          const std::size_t from = Sig(distinct + 1, end);
          if (from < end && IsWord(from, "from")) return operand(Sig(from + 1, end), Shape::kOperand, true);
        }
        return std::nullopt;
      }
      if (word == "to" && set && at == i) return operand(value, Shape::kOperand, false);
      return std::nullopt;
    };

    for (std::size_t i = first; i < end;) {
      if (ahead_at < ahead.size() && ahead[ahead_at].first <= i) {
        // A value found ahead: masked as one, and not read again.
        const auto [value, last] = ahead[ahead_at++];
        if (value >= i) {
          MaskValue(value, last);
          i = last;
          previous = last - 1;
          after = After::kNothing;
        }
        continue;
      }
      if (t_.kind(i) == Kind::kSpace) {
        i++;
        continue;
      }
      if (t_.kind(i) == Kind::kComment) {
        Comment(i);
        i++;
        continue;
      }
      const After was = after;
      after = After::kNothing;
      if (was != After::kNothing) {
        if (const auto next = value_after(i, was, 0)) {
          // Comments on the way (between the name and its value) are read like any other;
          // one inside the value goes with the value.
          for (std::size_t j = i + 1; j < *next && j < end; j++) {
            if (t_.kind(j) == Kind::kComment) Comment(j);
          }
          previous = i;
          i = std::max(*next, i + 1);
          continue;
        }
      }
      const Kind kind = t_.kind(i);
      const bool dotted = previous < end && t_.kind(previous) == Kind::kNumber && text(previous) == ".";
      if (kind == Kind::kWord || kind == Kind::kQuotedName) {
        if (!dotted) chain_before = previous;
        const std::string name = *NameAt(i);
        if (kind == Kind::kQuotedName) {
          // A quoted name can hold what a literal can: FROM "https://user:password@host/f.csv".
          if (auto masked = MaskBody(NameBody(text(i)), /*sql=*/false, nullptr)) Replace(i, i + 1, Quoted(*masked, text(i).front()));
        }
        if (IsCredentialName(name)) {
          if (!named.empty()) named.back() = true;
          after = After::kName;
          name_is_argument = chain_before < end && (IsOp(chain_before, "(") || IsOp(chain_before, ",")) &&
                             !calls.empty() && calls.back();
        } else if (IsKeyFunction(name)) {
          after = After::kKeyFunction;
        } else if (name == "identified" && kind == Kind::kWord) {
          Identified(i, end, ahead);
          std::sort(ahead.begin() + static_cast<std::ptrdiff_t>(ahead_at), ahead.end());
        } else if (name == "map" && kind == Kind::kWord) {
          MapLists(i, end, ahead);
          std::sort(ahead.begin() + static_cast<std::ptrdiff_t>(ahead_at), ahead.end());
        }
      } else if (kind == Kind::kString) {
        const std::string body = LiteralBody(text(i));
        const std::size_t next = Sig(i + 1, end);
        if (ComparedWithCredential(next, end) || IsAuthorizationValue(body)) {
          MaskValue(i, i + 1);  // 'value' = password; 'Bearer <token>'
        } else {
          const bool open = Literal(i);
          if (IsCredentialName(body)) after = After::kLiteralKey;  // {'password': '..'}
          if (open && next < end && IsOp(next, "||")) {
            // A connection string or URL put together: 'host=h password=' || <the password>.
            const std::size_t last = ValueEnd(next, end, Shape::kOperand);
            MaskValue(next, last);
            previous = i;
            i = std::max(last, i + 1);
            continue;
          }
          if (open && next < end && IsOp(next, ",") && !calls.empty() && calls.back()) {
            // format('https://h/f?token={}', <the token>): the other arguments.
            const std::size_t value = Sig(next + 1, end);
            const std::size_t last = ValueEnd(value, end, Shape::kGroup);
            MaskValue(value, last);
            previous = i;
            i = std::max(last, i + 1);
            continue;
          }
        }
      } else if (kind == Kind::kOperator) {
        if (Opens(i)) {
          const bool call = IsOp(i, "(") && previous < end &&
                            (t_.kind(previous) == Kind::kQuotedName ||
                             (t_.kind(previous) == Kind::kWord && !Among(kNotFunctions, Lower(text(previous)))));
          calls.push_back(call);
          named.push_back(false);
        } else if (Closes(i)) {
          // lower(password) = '..': the name still stands after the bracket.
          if (IsOp(i, ")") && !named.empty() && named.back()) after = After::kWrappedName;
          if (!calls.empty()) {
            calls.pop_back();
            named.pop_back();
          }
        }
      }
      previous = i;
      i++;
    }
  }

  // After a string literal: `= name` (or <>, !=, ==) where the name is a credential's and
  // not a function's.
  bool ComparedWithCredential(std::size_t op, std::size_t end) const {
    if (op >= end || t_.kind(op) != Kind::kOperator) return false;
    const std::string_view o = text(op);
    if (!(o == "=" || o == "==" || o == "<>" || o == "!=")) return false;
    std::size_t at = Sig(op + 1, end);
    std::optional<std::string> last;
    while (at < end && (t_.kind(at) == Kind::kWord || t_.kind(at) == Kind::kQuotedName)) {
      last = NameAt(at);
      const std::size_t dot = Sig(at + 1, end);
      if (dot >= end || !(t_.kind(dot) == Kind::kNumber && text(dot) == ".")) {
        at = dot;
        break;
      }
      at = Sig(dot + 1, end);
    }
    return last && IsCredentialName(*last) && !(at < end && IsOp(at, "("));
  }

  // IDENTIFIED BY 'password', IDENTIFIED WITH plugin BY 'password' / AS 'hash' (MySQL, Oracle).
  void Identified(std::size_t i, std::size_t end, std::vector<std::pair<std::size_t, std::size_t>>& ahead) const {
    std::size_t at = Sig(i + 1, end);
    if (at < end && IsWord(at, "with")) at = Sig(Sig(at + 1, end) + 1, end);
    if (at >= end || !(IsWord(at, "by") || IsWord(at, "as"))) return;
    const std::size_t value = Sig(at + 1, end);
    if (value >= end || IsWord(value, "password")) return;  // BY PASSWORD '..': the name's rule
    if (t_.kind(value) == Kind::kString || t_.kind(value) == Kind::kWord || t_.kind(value) == Kind::kQuotedName) {
      ahead.emplace_back(value, t_.kind(value) == Kind::kString ? ValueEnd(value, end, Shape::kOperand) : value + 1);
    }
  }

  const SqlTokens& t_;
  const std::size_t n_;
  const int depth_;
  Found found_;
  std::vector<Edit> edits_;
  std::size_t statement_begin_ = 0;
  mutable std::size_t counted_from_ = 0, counted_to_ = 0, counted_ = 0;  // OrdinalOf's
};

}  // namespace

MaskedSql MaskSql(std::string_view sql, std::size_t limit) {
  MaskedSql out;
  const SqlTokens tokens = LexSql(sql);
  if (tokens.unreadable()) {
    out.unreadable = tokens.unreadable();
    out.text = "[REDACTED: a statement that could not be read: " + *out.unreadable + "]";
    out.credentials = true;
    return out;
  }
  SqlMasker(tokens, 0).Run(out, limit);
  return out;
}

}  // namespace hardcharger
