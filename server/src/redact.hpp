// Credentials out of text the server shows or keeps: statements in logs, statement history,
// instrumentation and audit records, and the error messages that quote them.
//
// Two readers. Text that is not a statement (an error message, a connection string, what a
// string literal holds) is read by RedactCredentials, which knows the shapes credentials
// take there: key=value pairs and URLs. A statement is read by MaskSql as the tokens DuckDB
// itself makes of it (sql_lex.hpp), so a value is found where DuckDB finds it.
//
// What is masked is a list of forms, written out in README.md ("What the statement log
// masks"); a credential in a form outside the list is logged as written.
#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace hardcharger {

// Masks the credential values `text` may carry, idempotently (each value becomes ***):
//   - URL userinfo, `scheme://user:password@host`: the password, up to the last '@' before
//     the next white space (so a password with '@', ':', '/', a quote or a bracket in it goes
//     whole), and for http and https the user as well (`https://TOKEN@host`);
//   - key=value pairs whose key is a credential name (IsCredentialName): in libpq's keyword
//     form (unquoted to the next white space with backslash escapes, or '..' quoted), as
//     "..", {..} and ;-separated values (MySQL, ODBC, ADO, Azure connection strings), and as
//     URL query parameters (to the next '&' or '#'); also `key => value`, `key := value`;
//   - 'key': 'value' and "key": "value" when the key is quoted (JSON, DuckDB's structs);
//   - `Authorization: ...` and the other credential headers, to the end of the line.
// Everything else is kept, so the reason an error gives survives.
std::string RedactCredentials(std::string text);

// The same, after replacing each of `secrets` (whole connection strings the caller knows,
// as given and as rewritten) wherever it appears with "<connection string>". Empty
// entries are ignored.
std::string RedactCredentials(std::string text, const std::vector<std::string>& secrets);

// Whether a name is one whose value is a credential, whatever its case and whether it is
// written with '-' or '_': it ends in password, secret, token, api_key, authorization and
// the like (s3_secret_access_key, bearer_token, X-API-Key, Proxy-Authorization). The broad
// list: what a connection string's key, a setting, an option or a named argument is called.
// (A statement's columns, struct keys and name/value pairs use a narrower one, of the names
// only a credential has: README, "What the statement log masks".)
bool IsCredentialName(std::string_view name);

// A bind placeholder in a place whose value is masked (`token => ?`): the value bound to it
// is a credential too.
struct MaskedParameter {
  std::size_t ordinal = 0;  // of a `?` (counted from 1) or `$n`; 0 for a named one
  std::string name;         // of `$name`
};

// One statement as it may be logged, and what its masking hid.
struct MaskedStatement {
  // The statement with every credential value replaced by ***; a statement that is
  // credential DDL as a whole (CREATE SECRET, CREATE / ALTER USER with a password, SET of a
  // credential setting) or that DuckDB cannot tokenize is replaced by a line that says so.
  // Text longer than the limit ends at the last whole token that fits, then "…".
  std::string text;
  // The values that were hidden, as an error message could quote them: the literal's
  // value (quotes and escapes resolved), a connection string's password, a bare word.
  std::vector<std::string> secrets;
  std::vector<MaskedParameter> parameters;
  // Something was hidden (or a placeholder stands where a value would be).
  bool credentials = false;
  // A hidden value is computed (a function call, a subquery, an operator other than ||) or
  // there are too many to list: what an error would quote of it cannot be known.
  bool computed = false;
  // Why DuckDB cannot tokenize the text, when it cannot (SqlTokens::unreadable).
  std::optional<std::string> unreadable;
};

// A request's SQL: all of it, and each of its statements when it has several (in the order
// and number of the statements DuckDB's tokenizer splits it into; statements of only
// comments and white space are not counted).
struct MaskedSql : MaskedStatement {
  std::vector<MaskedStatement> statements;
};

// Reads `sql` once. `limit`: the longest text kept, for the request and for each statement.
MaskedSql MaskSql(std::string_view sql, std::size_t limit = 4000);

// `text` with every occurrence of each of `secrets` (and of each piece of one that has a
// delimiter in it) replaced by ***, whatever its case. A value shorter than four bytes is
// replaced only where it stands alone (not inside a word); a value of letters only, only
// where it stands alone and is written as it was hidden. Each value costs one pass.
std::string RedactSecrets(std::string text, const std::vector<std::string>& secrets);

// An error of `statement` as it may be logged and stored. DuckDB's excerpt of the statement
// ("LINE 1: ...") is dropped, every value the masking hid (and each of `bound`, the values
// bound to its masked placeholders) is replaced by ***, and what is left goes through
// RedactCredentials. When the hidden value is computed or the statement could not be read,
// nothing of the error can be trusted and a line that says which replaces it.
std::string RedactError(std::string_view error, const MaskedStatement& statement,
                        const std::vector<std::string>& bound = {});

// The credential values inside `value` (a bound parameter that is a URL or a connection
// string), for RedactError's `bound`.
std::vector<std::string> CredentialsIn(std::string_view value);

}  // namespace hardcharger
