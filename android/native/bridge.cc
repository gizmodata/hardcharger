#include "hardcharger_embedded.h"
#include <jni.h>
#include <openssl/pem.h>
#include <openssl/x509v3.h>
#include <openssl/rand.h>
#include <sys/stat.h>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace {
std::mutex server_mutex, console_mutex;
hardcharger_server* server = nullptr;
struct Console {
  hardcharger_console* value = nullptr;
  std::mutex run;
  ~Console() { if (value) hardcharger_console_close(value); }
};
std::shared_ptr<Console> console;
// JNI's modified UTF-8 does not encode supplementary Unicode as standard UTF-8.
std::string str(JNIEnv* env, jstring value) {
  if (!value) return {};
  auto cls = env->FindClass("java/lang/String");
  auto charset = env->NewStringUTF("UTF-8");
  auto bytes = static_cast<jbyteArray>(env->CallObjectMethod(value,
      env->GetMethodID(cls, "getBytes", "(Ljava/lang/String;)[B"), charset));
  std::string result(env->GetArrayLength(bytes), '\0');
  env->GetByteArrayRegion(bytes, 0, result.size(), reinterpret_cast<jbyte*>(result.data()));
  env->DeleteLocalRef(bytes); env->DeleteLocalRef(charset); env->DeleteLocalRef(cls);
  return result;
}
jstring string(JNIEnv* env, const std::string& value) {
  auto cls = env->FindClass("java/lang/String");
  auto bytes = env->NewByteArray(value.size());
  env->SetByteArrayRegion(bytes, 0, value.size(), reinterpret_cast<const jbyte*>(value.data()));
  auto charset = env->NewStringUTF("UTF-8");
  auto result = static_cast<jstring>(env->NewObject(cls,
      env->GetMethodID(cls, "<init>", "([BLjava/lang/String;)V"), bytes, charset));
  env->DeleteLocalRef(bytes); env->DeleteLocalRef(charset); env->DeleteLocalRef(cls);
  return result;
}
void fail(JNIEnv* env, const char* error) { env->ThrowNew(env->FindClass("java/lang/IllegalStateException"), error); }
std::shared_ptr<Console> current() { std::lock_guard lock(console_mutex); return console; }
}
#define JNI_METHOD(name) extern "C" JNIEXPORT name JNICALL
#define METHOD(name) Java_com_gizmodata_hardcharger_NativeBridge_##name
JNI_METHOD(jstring) METHOD(distribution)(JNIEnv* env, jobject) {
#ifdef HARDCHARGER_ANDROID_PLAY
  return string(env, "play");
#else
  return string(env, "local");
#endif
}
JNI_METHOD(jstring) METHOD(version)(JNIEnv* env, jobject) {
  return string(env, std::string(hardcharger_embedded_version()) + " · DuckDB " +
      hardcharger_embedded_duckdb_version() + " · Chargewire " + hardcharger_embedded_chargewire_version());
}
JNI_METHOD(jobjectArray) METHOD(versionDetails)(JNIEnv* env, jobject) {
  const char* versions[] = {hardcharger_embedded_version(), hardcharger_embedded_duckdb_version(),
      hardcharger_embedded_chargewire_version(), hardcharger_embedded_web_ui_version()};
  auto cls = env->FindClass("java/lang/String");
  auto result = env->NewObjectArray(4, cls, nullptr);
  env->DeleteLocalRef(cls);
  for (int i = 0; i < 4; ++i) {
    auto value = string(env, versions[i]);
    env->SetObjectArrayElement(result, i, value);
    env->DeleteLocalRef(value);
  }
  return result;
}
JNI_METHOD(jstring) METHOD(notices)(JNIEnv* env, jobject) {
  return string(env, hardcharger_embedded_third_party_notices());
}
JNI_METHOD(jint) METHOD(start)(JNIEnv* env, jobject, jobjectArray args, jstring password, jstring secret, jstring ca) {
  std::lock_guard lock(server_mutex);
  if (server) { fail(env, "The server is already running."); return 0; }
  std::vector<std::string> values;
  for (int i = 0; i < env->GetArrayLength(args); ++i) {
    auto value = static_cast<jstring>(env->GetObjectArrayElement(args, i));
    values.push_back(str(env, value)); env->DeleteLocalRef(value);
  }
  std::vector<const char*> argv;
  for (auto& value : values) argv.push_back(value.c_str());
  setenv("HARDCHARGER_ADMIN_PASSWORD", str(env, password).c_str(), 1);
  setenv("HARDCHARGER_SECRET_KEY", str(env, secret).c_str(), 1);
  setenv("SSL_CERT_FILE", str(env, ca).c_str(), 1);
  char error[4096] = {};
  int status = hardcharger_server_start(argv.size(), argv.data(), &server, error, sizeof(error));
  unsetenv("HARDCHARGER_ADMIN_PASSWORD"); unsetenv("HARDCHARGER_SECRET_KEY");
  if (status) { fail(env, error); return 0; }
  return hardcharger_server_port(server);
}
JNI_METHOD(void) METHOD(stop)(JNIEnv* env, jobject) {
  std::lock_guard lock(server_mutex);
  if (!server) return;
  hardcharger_server_request_stop(server, 0);
  char error[4096] = {};
  int status = hardcharger_server_wait(server, error, sizeof(error));
  hardcharger_server_free(server); server = nullptr;
  if (status) fail(env, error);
}
JNI_METHOD(jboolean) METHOD(running)(JNIEnv*, jobject) {
  std::lock_guard lock(server_mutex);
  return server && hardcharger_server_is_running(server);
}
JNI_METHOD(void) METHOD(openConsole)(JNIEnv* env, jobject, jstring host, jint port,
    jboolean tls, jstring roots, jstring username, jstring password, jstring token) {
  const auto h = str(env, host), r = str(env, roots), u = str(env, username), p = str(env, password), t = str(env, token);
  hardcharger_console_options opts{h.c_str(), port, tls, r.empty() ? nullptr : r.c_str(),
      u.c_str(), p.c_str(), t.empty() ? nullptr : t.c_str(), 100, 500};
  auto next = std::make_shared<Console>();
  char error[4096] = {};
  if (hardcharger_console_open(&opts, &next->value, error, sizeof(error))) { fail(env, error); return; }
  std::lock_guard lock(console_mutex); console = next;
}
JNI_METHOD(jstring) METHOD(runConsole)(JNIEnv* env, jobject, jstring sql) {
  auto active = current();
  if (!active) { fail(env, "Connect to a server first."); return nullptr; }
  std::lock_guard lock(active->run);
  int exited = 0;
  char* result = hardcharger_console_run(active->value, str(env, sql).c_str(), &exited);
  auto text = string(env, result ? result : "");
  hardcharger_console_free_text(result);
  return text;
}
JNI_METHOD(void) METHOD(cancelConsole)(JNIEnv*, jobject) {
  auto active = current(); if (active) hardcharger_console_cancel(active->value);
}
JNI_METHOD(void) METHOD(closeConsole)(JNIEnv*, jobject) {
  std::shared_ptr<Console> previous;
  { std::lock_guard lock(console_mutex); previous.swap(console); }
  if (previous) hardcharger_console_cancel(previous->value);
}
JNI_METHOD(jboolean) METHOD(complete)(JNIEnv* env, jobject, jstring sql) {
  int count = 0; return hardcharger_console_input_complete(str(env, sql).c_str(), &count);
}
JNI_METHOD(void) METHOD(makeCertificate)(JNIEnv* env, jobject, jstring directory, jstring names) {
  const auto path = str(env, directory), sans = str(env, names);
  using Key = std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)>;
  using Cert = std::unique_ptr<X509, decltype(&X509_free)>;
  Key key(EVP_EC_gen("prime256v1"), EVP_PKEY_free);
  Cert cert(X509_new(), X509_free);
  if (!key || !cert) { fail(env, "Could not create a TLS key."); return; }
  X509_set_version(cert.get(), 2);
  unsigned char serial[16];
  if (RAND_bytes(serial, sizeof(serial)) != 1) { fail(env, "Could not create a certificate serial."); return; }
  serial[0] &= 0x7f;
  BIGNUM* number = BN_bin2bn(serial, sizeof(serial), nullptr);
  BN_to_ASN1_INTEGER(number, X509_get_serialNumber(cert.get())); BN_free(number);
  X509_gmtime_adj(X509_getm_notBefore(cert.get()), -60);
  X509_gmtime_adj(X509_getm_notAfter(cert.get()), 365L * 24 * 60 * 60);
  X509_set_pubkey(cert.get(), key.get());
  auto subject = X509_get_subject_name(cert.get());
  X509_NAME_add_entry_by_txt(subject, "CN", MBSTRING_ASC,
      reinterpret_cast<const unsigned char*>("Hardcharger on Android"), -1, -1, 0);
  X509_set_issuer_name(cert.get(), subject);
  X509V3_CTX ctx{}; X509V3_set_ctx(&ctx, cert.get(), cert.get(), nullptr, nullptr, 0);
  for (const auto& item : std::vector<std::pair<int, std::string>>{
      {NID_basic_constraints, "critical,CA:FALSE"}, {NID_key_usage, "critical,digitalSignature"},
      {NID_ext_key_usage, "serverAuth"}, {NID_subject_alt_name, sans}}) {
    X509_EXTENSION* ext = X509V3_EXT_conf_nid(nullptr, &ctx, item.first, item.second.c_str());
    if (!ext) { fail(env, "Could not create certificate names."); return; }
    X509_add_ext(cert.get(), ext, -1); X509_EXTENSION_free(ext);
  }
  if (!X509_sign(cert.get(), key.get(), EVP_sha256())) { fail(env, "Could not sign certificate."); return; }
  FILE* k = fopen((path + "/server.key").c_str(), "w");
  if (!k) { fail(env, "Could not save TLS key."); return; }
  fchmod(fileno(k), 0600);
  const int written = PEM_write_PrivateKey(k, key.get(), nullptr, nullptr, 0, nullptr, nullptr); fclose(k);
  FILE* c = fopen((path + "/server.crt").c_str(), "w");
  if (!c) { fail(env, "Could not save certificate."); return; }
  const int cert_written = PEM_write_X509(c, cert.get()); fclose(c);
  if (!written || !cert_written) fail(env, "Could not write TLS certificate.");
}
