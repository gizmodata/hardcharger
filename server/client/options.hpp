// The client's command-line parser: long options (--name VALUE, --name=VALUE), short
// ones (-x VALUE, -xVALUE), switches, booleans (--name, --name=true|false), and one
// positional argument. Like
// Boost.ProgramOptions (which GizmoSQL 1.x used), an unknown option, a missing or
// malformed value, or an option given twice is an error.
#pragma once

#include <cctype>
#include <iostream>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace hardcharger::client {

class CommandLine {
 public:
  void Section(std::string title) { entries_.push_back({Kind::kSection, std::move(title), 0, "", "", {}}); }
  void Value(std::string name, char short_name, std::string metavar, std::string help, std::string* target) {
    entries_.push_back({Kind::kValue, std::move(name), short_name, std::move(metavar), std::move(help), {target, nullptr, nullptr}});
  }
  void Int(std::string name, char short_name, std::string metavar, std::string help, int* target) {
    entries_.push_back({Kind::kInt, std::move(name), short_name, std::move(metavar), std::move(help), {nullptr, target, nullptr}});
  }
  void Flag(std::string name, char short_name, std::string help, bool* target) {
    entries_.push_back({Kind::kFlag, std::move(name), short_name, "", std::move(help), {nullptr, nullptr, target}});
  }

  // A boolean: --name is true, --name=VALUE takes true/false/1/0 (any case). A value after a
  // space is not taken (it would read as the positional argument).
  void Bool(std::string name, char short_name, std::string help, bool* target) {
    entries_.push_back({Kind::kBool, std::move(name), short_name, "[=BOOL]", std::move(help), {nullptr, nullptr, target}});
  }
  // Parses argv; returns an error message, or nullopt. The positional argument (at most
  // one) goes to *positional.
  std::optional<std::string> Parse(int argc, char** argv, std::string* positional) {
    for (int i = 1; i < argc; i++) {
      std::string arg = argv[i];
      if (arg == "--") {
        for (i++; i < argc; i++) {
          if (auto e = Positional(argv[i], positional)) return e;
        }
        break;
      }
      const Entry* entry = nullptr;
      std::optional<std::string> inline_value;
      std::string shown;
      if (arg.size() > 2 && arg.starts_with("--")) {
        std::string name = arg.substr(2);
        if (auto eq = name.find('='); eq != std::string::npos) {
          inline_value = name.substr(eq + 1);
          name = name.substr(0, eq);
        }
        entry = FindLong(name);
        shown = "--" + name;
        if (entry == nullptr) return "unrecognised option '" + shown + "'";
      } else if (arg.size() >= 2 && arg[0] == '-' && arg[1] != '-') {
        entry = FindShort(arg[1]);
        shown = arg.substr(0, 2);
        if (entry == nullptr) return "unrecognised option '" + shown + "'";
        if (arg.size() > 2) {
          if (entry->kind == Kind::kFlag || entry->kind == Kind::kBool) return "option '" + shown + "' takes no value";
          inline_value = arg.substr(2);
        }
      } else {
        if (auto e = Positional(arg, positional)) return e;
        continue;
      }
      if (!given_.insert(entry->name).second) {
        return "option '--" + entry->name + "' cannot be specified more than once";
      }
      if (entry->kind == Kind::kFlag) {
        if (inline_value) return "option '--" + entry->name + "' takes no value";
        *entry->target.flag = true;
        continue;
      }
      if (entry->kind == Kind::kBool) {
        if (!inline_value) {
          *entry->target.flag = true;
          continue;
        }
        std::string lower = *inline_value;
        for (auto& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (lower == "true" || lower == "1") {
          *entry->target.flag = true;
        } else if (lower == "false" || lower == "0") {
          *entry->target.flag = false;
        } else {
          return "the argument ('" + *inline_value + "') for option '--" + entry->name +
                 "' is invalid: want true or false";
        }
        continue;
      }
      std::string value;
      if (inline_value) {
        value = *inline_value;
      } else if (i + 1 < argc) {
        value = argv[++i];
      } else {
        return "the required argument for option '--" + entry->name + "' is missing";
      }
      if (entry->kind == Kind::kInt) {
        try {
          size_t used = 0;
          const int number = std::stoi(value, &used);
          if (used != value.size()) throw std::invalid_argument(value);
          *entry->target.number = number;
        } catch (const std::exception&) {
          return "the argument ('" + value + "') for option '--" + entry->name + "' is invalid";
        }
      } else {
        *entry->target.text = value;
      }
    }
    return std::nullopt;
  }

  bool Given(const std::string& name) const { return given_.contains(name); }

  void PrintHelp(std::ostream& out) const {
    for (const auto& entry : entries_) {
      if (entry.kind == Kind::kSection) {
        out << "\n" << entry.name << ":\n";
        continue;
      }
      std::string left = "  ";
      left += entry.short_name != 0 ? std::string("-") + entry.short_name + ", " : "    ";
      left += "--" + entry.name;
      if (!entry.metavar.empty()) left += (entry.kind == Kind::kBool ? "" : " ") + entry.metavar;
      constexpr size_t kColumn = 32;
      if (left.size() + 2 > kColumn) {
        out << left << "\n" << std::string(kColumn, ' ');
      } else {
        out << left << std::string(kColumn - left.size(), ' ');
      }
      out << entry.help << "\n";
    }
  }

 private:
  enum class Kind { kSection, kValue, kInt, kFlag, kBool };
  struct Target {
    std::string* text;
    int* number;
    bool* flag;
  };
  struct Entry {
    Kind kind;
    std::string name;
    char short_name;
    std::string metavar;
    std::string help;
    Target target;
  };

  const Entry* FindLong(const std::string& name) const {
    for (const auto& entry : entries_) {
      if (entry.kind != Kind::kSection && entry.name == name) return &entry;
    }
    return nullptr;
  }
  const Entry* FindShort(char c) const {
    for (const auto& entry : entries_) {
      if (entry.kind != Kind::kSection && entry.short_name == c) return &entry;
    }
    return nullptr;
  }
  std::optional<std::string> Positional(const std::string& value, std::string* positional) {
    if (positional == nullptr || !positional->empty()) return "too many positional arguments ('" + value + "')";
    *positional = value;
    return std::nullopt;
  }

  std::vector<Entry> entries_;
  std::set<std::string> given_;
};

}  // namespace hardcharger::client
