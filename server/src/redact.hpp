// Credentials out of text the server shows or keeps: error messages that quote a
// connection string (DuckDB's postgres scanner quotes the whole ATTACH path, password
// included), statements in logs, anything an operator or a client may read.
#pragma once

#include <string>
#include <vector>

namespace hardcharger {

// Masks the credential values `text` may carry, idempotently:
//   - the password of a URL authority (scheme://user:password@host, up to the last '@'
//     of the address, so a password with an '@' in it goes too);
//   - key=value pairs for the usual credential keys, in libpq's keyword form
//     (password=..., quoted or not) and as URL query parameters (?password=...,
//     &sslpassword=...), and x-amz-* signing parameters;
//   - 'key': 'value' when the key is quoted (DuckDB's option lists).
// Each value becomes ***; everything else is kept, so the reason an error gives survives.
std::string RedactCredentials(std::string text);

// The same, after replacing each of `secrets` (whole connection strings the caller knows,
// as given and as rewritten) wherever it appears with "<connection string>". Empty
// entries are ignored.
std::string RedactCredentials(std::string text, const std::vector<std::string>& secrets);

}  // namespace hardcharger
