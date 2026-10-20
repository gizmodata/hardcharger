// Ported from GizmoSQL 1.x src/client/sql_processor.cpp.

#include "sql_processor.hpp"

#include <algorithm>
#include <cctype>

namespace hardcharger::client {

bool SqlProcessor::AccumulateLine(const std::string& line) {
  if (!buffer_.empty()) {
    buffer_ += '\n';
  }
  buffer_ += line;

  // Scan the newly added line for state changes
  for (size_t i = buffer_.size() - line.size(); i < buffer_.size(); ++i) {
    char c = buffer_[i];
    char next = (i + 1 < buffer_.size()) ? buffer_[i + 1] : '\0';

    if (in_block_comment_) {
      if (c == '*' && next == '/') {
        in_block_comment_ = false;
        ++i;  // skip '/'
      }
      continue;
    }

    if (in_single_quote_) {
      if (c == '\'' && next == '\'') {
        ++i;  // escaped quote
      } else if (c == '\'') {
        in_single_quote_ = false;
      }
      continue;
    }

    if (!dollar_tag_.empty()) {
      if (buffer_.compare(i, dollar_tag_.size(), dollar_tag_) == 0) {
        i += dollar_tag_.size() - 1;
        dollar_tag_.clear();
      }
      continue;
    }

    if (in_double_quote_) {
      if (c == '"' && next == '"') {
        ++i;  // escaped quote
      } else if (c == '"') {
        in_double_quote_ = false;
      }
      continue;
    }

    // Not in any quoted/comment context
    if (c == '\'') {
      in_single_quote_ = true;
    } else if (c == '"') {
      in_double_quote_ = true;
    } else if (c == '-' && next == '-') {
      // Line comment: rest of line is comment, stop processing
      break;
    } else if (c == '/' && next == '*') {
      in_block_comment_ = true;
      ++i;  // skip '*'
    } else if (c == '$') {
      // A dollar quote opens with $$ or $tag$ (tag: a letter or underscore, then letters,
      // digits and underscores), not inside an identifier; $1 is a parameter.
      const bool after_word = i > 0 && (std::isalnum(static_cast<unsigned char>(buffer_[i - 1])) ||
                                        buffer_[i - 1] == '_' || buffer_[i - 1] == '$');
      size_t end = i + 1;
      if (!after_word && end < buffer_.size() &&
          (std::isalpha(static_cast<unsigned char>(buffer_[end])) || buffer_[end] == '_' || buffer_[end] == '$')) {
        while (end < buffer_.size() &&
               (std::isalnum(static_cast<unsigned char>(buffer_[end])) || buffer_[end] == '_')) {
          ++end;
        }
        if (end < buffer_.size() && buffer_[end] == '$') {
          dollar_tag_ = buffer_.substr(i, end - i + 1);
          i = end;
        }
      }
    } else if (c == ';') {
      semicolon_pos_ = i;
      return true;  // Statement complete
    }
  }

  // Only comments so far (a standalone comment line, or what followed a statement's
  // semicolon): nothing to run, so nothing is pending. An open block comment stays
  // pending until it closes.
  if (!in_block_comment_ && !in_single_quote_ && !in_double_quote_ && dollar_tag_.empty() &&
      IsOnlyComments(buffer_)) {
    buffer_.clear();
  }
  return false;
}

bool SqlProcessor::IsOnlyComments(const std::string& text) {
  size_t i = 0;
  while (i < text.size()) {
    const char c = text[i];
    if (std::isspace(static_cast<unsigned char>(c))) {
      ++i;
    } else if (c == '-' && i + 1 < text.size() && text[i + 1] == '-') {
      const size_t end = text.find('\n', i);
      if (end == std::string::npos) return true;
      i = end + 1;
    } else if (c == '/' && i + 1 < text.size() && text[i + 1] == '*') {
      const size_t end = text.find("*/", i + 2);
      if (end == std::string::npos) return true;
      i = end + 2;
    } else {
      return false;
    }
  }
  return true;
}

std::string SqlProcessor::GetStatement() const {
  // Return everything up to (but not including) the semicolon
  std::string stmt;
  if (semicolon_pos_ != std::string::npos) {
    stmt = buffer_.substr(0, semicolon_pos_);
  } else {
    stmt = buffer_;
  }
  // Trim trailing whitespace
  while (!stmt.empty() && std::isspace(static_cast<unsigned char>(stmt.back()))) {
    stmt.pop_back();
  }
  // Comments alone ("/* note */;") are no statement.
  if (IsOnlyComments(stmt)) return "";
  return stmt;
}

std::string SqlProcessor::GetRemainder() const {
  if (semicolon_pos_ != std::string::npos && semicolon_pos_ + 1 < buffer_.size()) {
    std::string remainder = buffer_.substr(semicolon_pos_ + 1);
    // Trim leading whitespace
    size_t start = remainder.find_first_not_of(" \t\n\r");
    if (start == std::string::npos) return "";
    return remainder.substr(start);
  }
  return "";
}

void SqlProcessor::Reset() {
  buffer_.clear();
  semicolon_pos_ = std::string::npos;
  in_single_quote_ = false;
  in_double_quote_ = false;
  in_block_comment_ = false;
  dollar_tag_.clear();
}

bool SqlProcessor::IsAccumulating() const {
  return !buffer_.empty();
}

bool SqlProcessor::IsUpdateStatement(const std::string& sql) {
  // Find the first non-whitespace character
  std::string trimmed = sql;
  size_t start = trimmed.find_first_not_of(" \t\n\r");
  if (start == std::string::npos) return false;
  trimmed = trimmed.substr(start);

  // Convert first 10 chars to uppercase for prefix matching
  std::string prefix = trimmed.substr(0, 10);
  std::transform(prefix.begin(), prefix.end(), prefix.begin(), ::toupper);

  return prefix.find("INSERT") == 0 ||
         prefix.find("UPDATE") == 0 ||
         prefix.find("DELETE") == 0 ||
         prefix.find("CREATE") == 0 ||
         prefix.find("DROP") == 0 ||
         prefix.find("ALTER") == 0 ||
         prefix.find("BEGIN") == 0 ||
         prefix.find("COMMIT") == 0 ||
         prefix.find("ROLLBACK") == 0 ||
         prefix.find("SET") == 0 ||
         prefix.find("LOAD") == 0 ||
         prefix.find("INSTALL") == 0 ||
         prefix.find("COPY") == 0 ||
         prefix.find("ATTACH") == 0 ||
         prefix.find("DETACH") == 0 ||
         prefix.find("USE") == 0;
}

bool SqlProcessor::IsDdlStatement(const std::string& sql) {
  std::string trimmed = sql;
  size_t start = trimmed.find_first_not_of(" \t\n\r");
  if (start == std::string::npos) return false;
  trimmed = trimmed.substr(start);

  std::string prefix = trimmed.substr(0, 10);
  std::transform(prefix.begin(), prefix.end(), prefix.begin(), ::toupper);

  return prefix.find("CREATE") == 0 ||
         prefix.find("DROP") == 0 ||
         prefix.find("ALTER") == 0 ||
         prefix.find("ATTACH") == 0 ||
         prefix.find("DETACH") == 0 ||
         prefix.find("USE") == 0 ||
         prefix.find("CALL") == 0;
}

}  // namespace hardcharger::client
