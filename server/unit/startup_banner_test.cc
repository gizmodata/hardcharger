// The addresses the startup log prints (never the 0.0.0.0 a server binds), the banner's
// layout, and when log lines carry emoji.
#include <gtest/gtest.h>

#include <sstream>
#include <string>
#include <vector>

#include "logging.hpp"
#include "startup_banner.hpp"

using hardcharger::Banner;
using hardcharger::BannerInfo;
using hardcharger::IsWildcardHost;
using hardcharger::LogEmojiAuto;
using hardcharger::UsableBaseUrls;
using Urls = std::vector<std::string>;

namespace {

std::vector<std::string> Lines(const std::string& text) {
  std::vector<std::string> lines;
  std::istringstream in(text);
  for (std::string line; std::getline(in, line);) lines.push_back(line);
  return lines;
}

}  // namespace

TEST(UsableUrls, AWildcardBindIsLocalhostAndTheHostName) {
  for (const char* any : {"0.0.0.0", "::", "[::]", ""}) {
    EXPECT_TRUE(IsWildcardHost(any)) << any;
    EXPECT_EQ(UsableBaseUrls(any, 31337, false, {}, "build-box"),
              (Urls{"http://localhost:31337", "http://build-box:31337"}))
        << any;
  }
  // No host name, or one that is localhost: once.
  EXPECT_EQ(UsableBaseUrls("0.0.0.0", 8080, false, {}, ""), (Urls{"http://localhost:8080"}));
  EXPECT_EQ(UsableBaseUrls("0.0.0.0", 8080, false, {}, "LOCALHOST"), (Urls{"http://localhost:8080"}));
}

TEST(UsableUrls, ASpecificBindIsItself) {
  EXPECT_FALSE(IsWildcardHost("127.0.0.1"));
  EXPECT_EQ(UsableBaseUrls("127.0.0.1", 31337, false, {}, "box"), (Urls{"http://127.0.0.1:31337"}));
  EXPECT_EQ(UsableBaseUrls("db.example.com", 443, true, {"other.example.com"}, "box"),
            (Urls{"https://db.example.com:443"}));
  EXPECT_EQ(UsableBaseUrls("::1", 31337, false, {}, "box"), (Urls{"http://[::1]:31337"}));
  EXPECT_EQ(UsableBaseUrls("[::1]", 31337, false, {}, "box"), (Urls{"http://[::1]:31337"}));
}

TEST(UsableUrls, TlsPrefersTheCertificatesName) {
  // Its first DNS name that isn't a wildcard, then localhost when it names that too.
  EXPECT_EQ(UsableBaseUrls("0.0.0.0", 31337, true, {"*.example.com", "db.example.com", "localhost"}, "box"),
            (Urls{"https://db.example.com:31337", "https://localhost:31337"}));
  EXPECT_EQ(UsableBaseUrls("0.0.0.0", 31337, true, {"localhost"}, "box"), (Urls{"https://localhost:31337"}));
  // No usable name: the host name, else localhost.
  EXPECT_EQ(UsableBaseUrls("::", 31337, true, {"*.example.com"}, "box"), (Urls{"https://box:31337"}));
  EXPECT_EQ(UsableBaseUrls("0.0.0.0", 31337, true, {}, ""), (Urls{"https://localhost:31337"}));
}

TEST(StartupBanner, TheArtThenVersionsThenTheUrlsAndDocs) {
  const std::string banner = Banner({.version = "2.0.0-rc.2",
                                     .chargewire_version = "1.0.0",
                                     .duckdb_version = "v2.0.0",
                                     .label = "UI",
                                     .urls = {"http://localhost:31337/ui/", "http://build-box:31337/ui/"}});
  const auto lines = Lines(banner);
  ASSERT_TRUE(banner.starts_with(hardcharger::kBannerArt));
  ASSERT_EQ(lines.size(), 9u) << banner;
  EXPECT_EQ(lines[6], "  Hardcharger 2.0.0-rc.2 | Chargewire 1.0.0 | DuckDB v2.0.0 | by GizmoData");
  EXPECT_EQ(lines[7], "  UI   http://localhost:31337/ui/      Docs  docs.hardcharger.gizmodata.com");
  EXPECT_EQ(lines[8], "       http://build-box:31337/ui/");
  for (const auto& line : lines) {
    EXPECT_LE(line.size(), 80u) << line;
    for (unsigned char c : line) EXPECT_TRUE(c >= 0x20 && c < 0x7f) << line;
  }
}

TEST(StartupBanner, TheDocsGoOnALineOfTheirOwnWhenAUrlIsLong) {
  const std::string url = "https://a-rather-long-host-name.us-east-1.compute.example.com:31337/chargewire";
  const auto lines = Lines(Banner(
      {.version = "2.0.0", .chargewire_version = "1.0.0", .duckdb_version = "v2.0.0", .label = "Chargewire", .urls = {url}}));
  ASSERT_EQ(lines.size(), 9u);
  EXPECT_EQ(lines[7], "  Chargewire  " + url);
  EXPECT_EQ(lines[8], "  Docs        https://docs.hardcharger.gizmodata.com");
  // A short one keeps them together, with the docs' whole URL when it fits.
  const auto short_lines = Lines(Banner({.version = "2.0.0",
                                         .chargewire_version = "1.0.0",
                                         .duckdb_version = "v2.0.0",
                                         .label = "Chargewire",
                                         .urls = {"http://h:1/ui/"}}));
  ASSERT_EQ(short_lines.size(), 8u);
  EXPECT_EQ(short_lines[7], "  Chargewire  http://h:1/ui/      Docs  https://docs.hardcharger.gizmodata.com");
}

TEST(LogEmoji, AutoNeedsATerminalAndAUtf8Locale) {
#ifndef _WIN32
  EXPECT_TRUE(LogEmojiAuto(true, nullptr, nullptr, "en_US.UTF-8"));
  EXPECT_TRUE(LogEmojiAuto(true, nullptr, "C.utf8", "C"));
  EXPECT_FALSE(LogEmojiAuto(true, nullptr, nullptr, "C"));
  EXPECT_FALSE(LogEmojiAuto(true, nullptr, nullptr, nullptr));
  // LC_ALL wins over the rest; an empty one counts as unset.
  EXPECT_FALSE(LogEmojiAuto(true, "C", "en_US.UTF-8", "en_US.UTF-8"));
  EXPECT_TRUE(LogEmojiAuto(true, "", "", "de_DE.UTF-8"));
#endif
  // Off a terminal (a file, a pipe, the journal, a container's log): never.
  EXPECT_FALSE(LogEmojiAuto(false, "en_US.UTF-8", "en_US.UTF-8", "en_US.UTF-8"));
}
