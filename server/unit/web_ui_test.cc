// ServeWebUi: the UI's serving rules, on a file list of the test's own with real gzip bytes
// (the server's pytest suite, test_web_ui.py, checks them on the embedded UI over HTTP).
#include "web_ui.hpp"

#include <gtest/gtest.h>
#include <zlib.h>

#include <string>
#include <vector>

namespace hardcharger {
namespace {

std::vector<unsigned char> Gzip(const std::string& text) {
  z_stream stream{};
  EXPECT_EQ(deflateInit2(&stream, 9, Z_DEFLATED, 16 + MAX_WBITS, 8, Z_DEFAULT_STRATEGY), Z_OK);
  std::vector<unsigned char> out(deflateBound(&stream, text.size()) + 32);
  stream.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(text.data()));
  stream.avail_in = static_cast<uInt>(text.size());
  stream.next_out = out.data();
  stream.avail_out = static_cast<uInt>(out.size());
  EXPECT_EQ(deflate(&stream, Z_FINISH), Z_STREAM_END);
  out.resize(stream.total_out);
  deflateEnd(&stream);
  return out;
}

std::string Gunzip(const std::string& gz) {
  z_stream stream{};
  EXPECT_EQ(inflateInit2(&stream, 16 + MAX_WBITS), Z_OK);
  std::string out(1 << 20, '\0');
  stream.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(gz.data()));
  stream.avail_in = static_cast<uInt>(gz.size());
  stream.next_out = reinterpret_cast<Bytef*>(out.data());
  stream.avail_out = static_cast<uInt>(out.size());
  EXPECT_EQ(inflate(&stream, Z_FINISH), Z_STREAM_END);
  out.resize(stream.total_out);
  inflateEnd(&stream);
  return out;
}

const std::string kIndexText = std::string("<!doctype html><html>") + std::string(2000, 'x') + "</html>";
const std::string kScriptText = std::string("console.log(1);") + std::string(3000, ';');
const std::vector<unsigned char> kIndexGz = Gzip(kIndexText);
const std::vector<unsigned char> kScriptGz = Gzip(kScriptText);
const unsigned char kPng[] = {0x89, 'P', 'N', 'G'};

const WebUiFile kFiles[] = {
    {"index.html", kIndexGz.data(), kIndexGz.size(), kIndexText.size(), true,
     "1111111111111111111111111111111111111111111111111111111111111111"},
    {"assets/index-1a2b3c4d.js", kScriptGz.data(), kScriptGz.size(), kScriptText.size(), true,
     "2222222222222222222222222222222222222222222222222222222222222222"},
    {"favicon.png", kPng, sizeof(kPng), sizeof(kPng), false,
     "3333333333333333333333333333333333333333333333333333333333333333"},
};

warpsql::PlainRequest Get(const std::string& path, std::vector<std::pair<std::string, std::string>> headers = {}) {
  return {"GET", path, std::move(headers)};
}

std::string Header(const warpsql::PlainResponse& r, std::string_view name) {
  for (const auto& [k, v] : r.headers) {
    if (k == name) return v;
  }
  return "";
}

TEST(WebUi, IndexAndTheAppsOwnRoutes) {
  for (const char* path : {"/ui/", "/ui/sessions", "/ui/a/deeper/route", "/ui/query/"}) {
    auto r = ServeWebUi(kFiles, Get(path));
    ASSERT_TRUE(r) << path;
    EXPECT_EQ(r->status, 200) << path;
    EXPECT_EQ(r->body, kIndexText) << path;
    EXPECT_EQ(r->content_type, "text/html; charset=utf-8");
    EXPECT_EQ(Header(*r, "Cache-Control"), "no-cache");
    EXPECT_EQ(Header(*r, "Content-Security-Policy"), kWebUiContentSecurityPolicy);
    EXPECT_EQ(Header(*r, "X-Content-Type-Options"), "nosniff");
    EXPECT_EQ(Header(*r, "X-Frame-Options"), "DENY");
  }
}

TEST(WebUi, GzipAndIdentityAnswersAreTheSameBytes) {
  auto gz = ServeWebUi(kFiles, Get("/ui/assets/index-1a2b3c4d.js", {{"Accept-Encoding", "gzip, deflate, br"}}));
  auto plain = ServeWebUi(kFiles, Get("/ui/assets/index-1a2b3c4d.js", {{"Accept-Encoding", "identity"}}));
  auto none = ServeWebUi(kFiles, Get("/ui/assets/index-1a2b3c4d.js"));
  ASSERT_TRUE(gz && plain && none);
  EXPECT_EQ(Header(*gz, "Content-Encoding"), "gzip");
  EXPECT_LT(gz->body.size(), kScriptText.size());
  EXPECT_EQ(Gunzip(gz->body), kScriptText);
  EXPECT_EQ(Header(*plain, "Content-Encoding"), "");
  EXPECT_EQ(plain->body, kScriptText);
  EXPECT_EQ(none->body, kScriptText);  // no Accept-Encoding at all: identity
  for (const auto* r : {&*gz, &*plain, &*none}) {
    EXPECT_EQ(Header(*r, "Vary"), "Accept-Encoding");
    EXPECT_EQ(r->content_type, "text/javascript; charset=utf-8");
    EXPECT_EQ(Header(*r, "Cache-Control"), "public, max-age=31536000, immutable");
  }
  // Each representation has its own strong ETag.
  EXPECT_EQ(Header(*plain, "ETag"), "\"22222222222222222222222222222222\"");
  EXPECT_EQ(Header(*gz, "ETag"), "\"22222222222222222222222222222222-gzip\"");
}

TEST(WebUi, AFileGzipWouldNotShrinkIsServedAsItIs) {
  for (const char* accept : {"gzip", ""}) {
    auto r = ServeWebUi(kFiles, Get("/ui/favicon.png", {{"Accept-Encoding", accept}}));
    ASSERT_TRUE(r);
    EXPECT_EQ(r->body, std::string(reinterpret_cast<const char*>(kPng), sizeof(kPng)));
    EXPECT_EQ(Header(*r, "Content-Encoding"), "");
    EXPECT_EQ(Header(*r, "Vary"), "");
    EXPECT_EQ(r->content_type, "image/png");
    EXPECT_EQ(Header(*r, "Cache-Control"), "no-cache");
  }
}

TEST(WebUi, AcceptEncodingIsReadAsRfc9110Says) {
  EXPECT_TRUE(AcceptsGzip("gzip"));
  EXPECT_TRUE(AcceptsGzip("GZIP"));
  EXPECT_TRUE(AcceptsGzip("deflate, gzip;q=0.5"));
  EXPECT_TRUE(AcceptsGzip("x-gzip"));
  EXPECT_TRUE(AcceptsGzip("*"));
  EXPECT_TRUE(AcceptsGzip("br;q=1.0, *;q=0.1"));
  EXPECT_FALSE(AcceptsGzip(""));
  EXPECT_FALSE(AcceptsGzip("identity"));
  EXPECT_FALSE(AcceptsGzip("gzip;q=0"));
  EXPECT_FALSE(AcceptsGzip("gzip; q=0.000"));
  EXPECT_FALSE(AcceptsGzip("*, gzip;q=0"));
  EXPECT_FALSE(AcceptsGzip("*;q=0"));
  EXPECT_FALSE(AcceptsGzip("br, deflate"));
}

TEST(WebUi, IfNoneMatchAnswers304ForThatRepresentationOnly) {
  const std::string gz_tag = "\"22222222222222222222222222222222-gzip\"";
  auto same = ServeWebUi(kFiles, Get("/ui/assets/index-1a2b3c4d.js", {{"Accept-Encoding", "gzip"},
                                                                       {"If-None-Match", gz_tag}}));
  ASSERT_TRUE(same);
  EXPECT_EQ(same->status, 304);
  EXPECT_TRUE(same->body.empty());
  EXPECT_EQ(Header(*same, "ETag"), gz_tag);
  EXPECT_EQ(Header(*same, "Vary"), "Accept-Encoding");
  // The gzip tag doesn't validate the identity representation.
  auto other = ServeWebUi(kFiles, Get("/ui/assets/index-1a2b3c4d.js", {{"If-None-Match", gz_tag}}));
  ASSERT_TRUE(other);
  EXPECT_EQ(other->status, 200);
  EXPECT_EQ(other->body, kScriptText);
  auto weak = ServeWebUi(kFiles, Get("/ui/", {{"If-None-Match", "W/\"x\", W/\"11111111111111111111111111111111\""}}));
  ASSERT_TRUE(weak);
  EXPECT_EQ(weak->status, 304);
}

TEST(WebUi, MissingFilesAreNotFoundAndNeverCached) {
  for (const char* path : {"/ui/assets/gone-1a2b3c4d.js", "/ui/assets/route", "/ui/missing.png", "/ui/../warp",
                           "/ui/assets/../index.html", "/ui/./index.html", "/ui//index.html"}) {
    auto r = ServeWebUi(kFiles, Get(path));
    ASSERT_TRUE(r) << path;
    EXPECT_EQ(r->status, 404) << path;
    EXPECT_EQ(Header(*r, "Cache-Control"), "no-store") << path;
    EXPECT_EQ(Header(*r, "Content-Security-Policy"), kWebUiContentSecurityPolicy) << path;
  }
}

TEST(WebUi, OnlyItsOwnPaths) {
  auto redirect = ServeWebUi(kFiles, Get("/ui"));
  ASSERT_TRUE(redirect);
  EXPECT_EQ(redirect->status, 308);
  EXPECT_EQ(Header(*redirect, "Location"), "/ui/");
  for (const char* path : {"/", "/uix", "/u", "/index.html", "/favicon.png", "/health"}) {
    EXPECT_FALSE(ServeWebUi(kFiles, Get(path))) << path;
  }
}

TEST(WebUi, WithoutFilesEverythingIsNotFound) {
  auto r = ServeWebUi({}, Get("/ui/"));
  ASSERT_TRUE(r);
  EXPECT_EQ(r->status, 404);
}

}  // namespace
}  // namespace hardcharger
