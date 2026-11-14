// Credentials out of text the server shows or keeps: error messages that quote a
// connection string (DuckDB's postgres scanner quotes the whole ATTACH path, password
// included), statements in logs, anything an operator or a client may read.
#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

// Masks the credential values `text` may carry, idempotently:
//   - the password of a URL authority (scheme://user:password@host, up to the last '@'
//     of the address, so a password with an '@' in it goes too);
//   - key=value pairs for the usual credential keys (IsCredentialName), in libpq's keyword
//     form (password=..., quoted or not) and as URL query parameters (?password=...,
//     &sslpassword=..., ?key=...), and x-amz-* signing parameters;
//   - 'key': 'value' when the key is quoted (DuckDB's option lists).
// Each value becomes ***; everything else is kept, so the reason an error gives survives.
std::string RedactCredentials(std::string text);

// The same, after replacing each of `secrets` (whole connection strings the caller knows,
// as given and as rewritten) wherever it appears with "<connection string>". Empty
// entries are ignored.
std::string RedactCredentials(std::string text, const std::vector<std::string>& secrets);

// Whether a name (lower case, unquoted) is one whose value is a credential: it ends in
// password, secret, token, api_key and the like (s3_secret_access_key, bearer_token).
bool IsCredentialName(std::string_view name);

// A statement as it may be logged. It is read as tokens (comments, every kind of string
// literal and quoted names understood), never as raw text:
//   - the value given to a credential name is ***, whatever joins the two: `name => v`,
//     `name := v`, `name = v` (and the other comparisons), `name: v`, `'name': v`, or an
//     option list's `NAME 'v'`, with any spacing or comments between, and whether the value
//     is one literal or an expression. A bind placeholder (?, $1) is kept;
//   - what a string literal or a quoted name holds goes through RedactCredentials (an
//     ATTACH connection string, a URL);
//   - comment bodies and escaped literals are concealed conservatively;
//   - text longer than `limit` bytes ends at the last whole token that fits, then "…".
// A statement that cannot be read (a string, quoted name or comment that never ends) is
// `unreadable`, with no text: nothing of it may be logged.
struct RedactedSql {
  std::string text;
  bool unreadable = false;
};
RedactedSql RedactSql(std::string_view sql, std::size_t limit);

// Whether any part of the complete statement is sensitive, including placeholders,
// comments, escaped literals and connection strings, even beyond the display limit.
// None when unreadable. Callers must fail closed on that result.
std::optional<bool> SqlHasCredentials(std::string_view sql);

}  // namespace hardcharger
