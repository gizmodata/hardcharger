// ServeWebUi: hardcharger-ui's serving rules, on a file list of the test's own (the
// server's pytest suite, test_web_ui.py, checks them on the embedded UI over HTTP).
#include "web_ui.hpp"

#include <gtest/gtest.h>

#include <string>

namespace hardcharger {
namespace {

const unsigned char kIndex[] = {'<', 'h', 't', 'm', 'l', '>'};
const unsigned char kScript[] = {'1', ';'};
const unsigned char kWasm[] = {0, 'a', 's', 'm'};
const WebUiFile kFiles[] = {
    {"index.html", kIndex, sizeof(kIndex)},
    {"assets/index-1a2b3c4d.js", kScript, sizeof(kScript)},
    {"assets/parquet_wasm_bg-1a2b3c4d.wasm", kWasm, sizeof(kWasm)},
    {"favicon.png", kScript, sizeof(kScript)},
};

std::string Header(const warpsql::PlainResponse& r, std::string_view name) {
  for (const auto& [k, v] : r.headers) {
    if (k == name) return v;
  }
  return "";
}

TEST(WebUi, IndexAndTheAppsOwnRoutes) {
  for (const char* path : {"/ui/", "/ui/sessions", "/ui/a/deeper/route", "/ui/query/"}) {
    auto r = ServeWebUi(kFiles, path);
    ASSERT_TRUE(r) << path;
    EXPECT_EQ(r->status, 200) << path;
    EXPECT_EQ(r->body, "<html>") << path;
    EXPECT_EQ(r->content_type, "text/html; charset=utf-8");
    EXPECT_EQ(Header(*r, "Cache-Control"), "no-cache");
    EXPECT_EQ(Header(*r, "Content-Security-Policy"), kWebUiContentSecurityPolicy);
    EXPECT_EQ(Header(*r, "X-Content-Type-Options"), "nosniff");
    EXPECT_EQ(Header(*r, "X-Frame-Options"), "DENY");
  }
}

TEST(WebUi, HashedAssetsAreImmutableWithTheirTypes) {
  auto js = ServeWebUi(kFiles, "/ui/assets/index-1a2b3c4d.js");
  ASSERT_TRUE(js);
  EXPECT_EQ(js->status, 200);
  EXPECT_EQ(js->content_type, "text/javascript; charset=utf-8");
  EXPECT_EQ(Header(*js, "Cache-Control"), "public, max-age=31536000, immutable");
  auto wasm = ServeWebUi(kFiles, "/ui/assets/parquet_wasm_bg-1a2b3c4d.wasm");
  ASSERT_TRUE(wasm);
  EXPECT_EQ(wasm->content_type, "application/wasm");
  EXPECT_EQ(wasm->body.size(), 4u);
  auto favicon = ServeWebUi(kFiles, "/ui/favicon.png");
  ASSERT_TRUE(favicon);
  EXPECT_EQ(favicon->content_type, "image/png");
  EXPECT_EQ(Header(*favicon, "Cache-Control"), "no-cache");
}

TEST(WebUi, MissingFilesAreNotFoundAndNeverCached) {
  for (const char* path : {"/ui/assets/gone-1a2b3c4d.js", "/ui/assets/route", "/ui/missing.png", "/ui/../warp",
                           "/ui/assets/../index.html", "/ui/./index.html", "/ui//index.html"}) {
    auto r = ServeWebUi(kFiles, path);
    ASSERT_TRUE(r) << path;
    EXPECT_EQ(r->status, 404) << path;
    EXPECT_EQ(Header(*r, "Cache-Control"), "no-store") << path;
    EXPECT_EQ(Header(*r, "Content-Security-Policy"), kWebUiContentSecurityPolicy) << path;
  }
}

TEST(WebUi, OnlyItsOwnPaths) {
  auto redirect = ServeWebUi(kFiles, "/ui");
  ASSERT_TRUE(redirect);
  EXPECT_EQ(redirect->status, 308);
  EXPECT_EQ(Header(*redirect, "Location"), "/ui/");
  for (const char* path : {"/", "/uix", "/u", "/index.html", "/favicon.png", "/health"}) {
    EXPECT_FALSE(ServeWebUi(kFiles, path)) << path;
  }
}

TEST(WebUi, WithoutFilesEverythingIsNotFound) {
  auto r = ServeWebUi({}, "/ui/");
  ASSERT_TRUE(r);
  EXPECT_EQ(r->status, 404);
}

}  // namespace
}  // namespace hardcharger
