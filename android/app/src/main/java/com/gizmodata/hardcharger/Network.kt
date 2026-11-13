package com.gizmodata.hardcharger

import android.util.Base64
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.UUID
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

data class Endpoint(val origin: String, val certificatePEM: String = "") {
    val url get() = origin.toHttpUrl()
    val tls get() = url.isHttps
    val fingerprint: String get() = if (certificatePEM.isEmpty()) "" else fingerprint(certificate(certificatePEM))
    companion object {
        fun parse(text: String): Endpoint {
            val input = text.trim().let { if ("://" in it) it else "https://$it" }
            val url = input.toHttpUrl()
            require(url.username.isEmpty() && url.password.isEmpty()) { "Enter credentials in their own fields." }
            require(url.scheme in listOf("https", "http")) { "Use an HTTP or HTTPS address." }
            val result = url.newBuilder().encodedPath("/").query(null).fragment(null).build().toString().trimEnd('/')
            return Endpoint(result)
        }
    }
}

data class RemoteServer(val id: String = UUID.randomUUID().toString(), val name: String,
    val endpoint: Endpoint, val username: String = "", val password: String = "", val browser: Boolean = false,
    val expectedFingerprint: String = "") {
    fun json() = JSONObject().put("id", id).put("name", name).put("origin", endpoint.origin)
        .put("certificate", endpoint.certificatePEM).put("username", username).put("password", password).put("browser", browser)
        .put("expectedFingerprint", expectedFingerprint)
    companion object {
        fun fromJson(j: JSONObject) = RemoteServer(j.getString("id"), j.getString("name"),
            Endpoint(j.getString("origin"), j.optString("certificate")), j.optString("username"), j.optString("password"), j.optBoolean("browser"), j.optString("expectedFingerprint"))
    }
}

fun connectionLink(uri: android.net.Uri): RemoteServer {
    require(uri.scheme == "hardcharger" && uri.host == "connect") { "This is not a Hardcharger connection link." }
    val endpoint = Endpoint.parse(uri.getQueryParameter("url") ?: error("The connection link has no server URL."))
    val pin = uri.getQueryParameter("fingerprint").orEmpty().uppercase()
    require(pin.isEmpty() || Regex("[0-9A-F]{2}(:[0-9A-F]{2}){31}").matches(pin)) { "The connection link has an invalid certificate fingerprint." }
    require(pin.isEmpty() || endpoint.tls) { "Certificate fingerprints require HTTPS." }
    return RemoteServer(name = uri.getQueryParameter("name") ?: endpoint.url.host, endpoint = endpoint,
        username = uri.getQueryParameter("username") ?: "hardcharger", expectedFingerprint = pin)
}

data class AccessToken(val token: String, val username: String, val expiresAt: Long) {
    fun fresh() = expiresAt > System.currentTimeMillis() + 300_000
    fun payload() = JSONObject().put("token", token).put("username", username).put("expiresAt", expiresAt)
}
data class BrowserFlow(val id: String, val secret: String, val url: String, val intervalMillis: Long)
class CertificateApproval(val pem: String, cause: Throwable) : Exception("This server's certificate is not trusted. Verify its fingerprint with the administrator.", cause)
fun certificate(pem: String) = CertificateFactory.getInstance("X.509").generateCertificate(pem.byteInputStream()) as X509Certificate
fun fingerprint(cert: X509Certificate): String = MessageDigest.getInstance("SHA-256").digest(cert.encoded).joinToString(":") { "%02X".format(it) }
fun pem(cert: X509Certificate) = "-----BEGIN CERTIFICATE-----\n" + Base64.encodeToString(cert.encoded, Base64.NO_WRAP).chunked(64).joinToString("\n") + "\n-----END CERTIFICATE-----\n"

/** Trust one approved certificate, with validity and hostname checks still enforced. */
class ChargewireClient(val endpoint: Endpoint) {
    private var protocolRoot = "chargewire"
    private var presented: X509Certificate? = null
    fun presentedFingerprint(): String = presented?.let { fingerprint(it) }.orEmpty()
    private val systemTrust = (TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
        init(null as KeyStore?)
    }.trustManagers.filterIsInstance<X509TrustManager>().first())
    private val trust = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = systemTrust.acceptedIssuers
        override fun checkClientTrusted(chain: Array<out X509Certificate>, authType: String) = systemTrust.checkClientTrusted(chain, authType)
        override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String) {
            val leaf = chain.firstOrNull() ?: error("Server presented no certificate.")
            presented = leaf
            if (endpoint.certificatePEM.isNotEmpty()) {
                leaf.checkValidity()
                require(MessageDigest.isEqual(leaf.encoded, certificate(endpoint.certificatePEM).encoded)) { "The server certificate has changed. Edit this connection to review trust." }
            } else systemTrust.checkServerTrusted(chain, authType)
        }
    }
    val http: OkHttpClient = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(45, TimeUnit.SECONDS).callTimeout(60, TimeUnit.SECONDS)
        .followRedirects(false).followSslRedirects(false)
        .sslSocketFactory(SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }.socketFactory, trust).build()
    fun request(path: String, method: String = "GET", body: JSONObject? = null, token: AccessToken? = null): JSONObject {
        val builder = Request.Builder().url("${endpoint.origin}/$protocolRoot/$path".trimEnd('/')).header("Accept", "application/json")
        builder.header("Prefer", "wait=30")
        if (token != null) builder.header("Authorization", "Bearer ${token.token}")
        if (method != "GET") builder.method(method, (body ?: JSONObject()).toString().toRequestBody("application/json".toMediaType()))
        try {
            http.newCall(builder.build()).execute().use { response ->
                val text = response.body?.string().orEmpty()
                val json = if (text.isBlank()) JSONObject() else JSONObject(text)
                check(response.isSuccessful) { json.optString("detail", json.optString("title", "Server returned HTTP ${response.code}.")) }
                return json
            }
        } catch (error: Exception) {
            val leaf = presented
            if (endpoint.certificatePEM.isEmpty() && leaf != null &&
                generateSequence<Throwable>(error) { it.cause }.any { it is javax.net.ssl.SSLHandshakeException }) {
                // Do not offer to override expiry. The retry also verifies the hostname.
                leaf.checkValidity()
                throw CertificateApproval(pem(leaf), error)
            }
            throw error
        }
    }
    fun discover(): JSONObject {
        try {
            val current = request("")
            if (current.optString("protocol") == "chargewire") return current
        } catch (trust: CertificateApproval) { throw trust }
        catch (transport: java.io.IOException) { throw transport }
        catch (_: Exception) { /* Older servers published discovery under /warp. */ }
        protocolRoot = "warp"
        val legacy = request("")
        check(legacy.optString("protocol") == "warp-sql") { "This address does not serve the Hardcharger protocol." }
        return legacy
    }
    fun signIn(username: String, password: String): AccessToken = token(request("v1/auth/token", "POST",
        JSONObject().put("grant_type", "password").put("username", username).put("password", password)), username)
    fun beginBrowser(): BrowserFlow {
        val j = request("v1/auth/oauth/flows", "POST")
        val authorization = j.getString("authorize_url").toHttpUrl()
        require(authorization.isHttps || (!endpoint.tls && authorization.host == endpoint.url.host)) { "The sign-in provider must use HTTPS." }
        return BrowserFlow(j.getString("flow_id"), j.getString("poll_secret"), authorization.toString(), j.optLong("poll_interval_s", 2).coerceIn(1, 10) * 1000)
    }
    fun pollBrowser(flow: BrowserFlow): AccessToken? {
        val j = request("v1/auth/oauth/flows/${flow.id}", "POST", JSONObject().put("poll_secret", flow.secret))
        return when (j.getString("state")) {
            "complete" -> token(j, "")
            "failed" -> error(j.optJSONObject("error")?.optString("detail") ?: "Browser sign-in failed.")
            else -> null
        }
    }
    fun execute(sql: String, token: AccessToken): Long? {
        val session = request("v1/sessions", "POST", token = token).getString("session_id")
        try {
            var result = request("v1/sessions/$session/statements", "POST", JSONObject().put("sql", sql), token)
            while (result.optString("state") in listOf("queued", "running")) {
                result = request("v1/sessions/$session/statements/${result.getString("statement_id")}", token = token)
            }
            check(result.optString("state") == "succeeded") { result.optJSONObject("error")?.optString("detail") ?: "Import did not succeed." }
            return result.optJSONObject("result")?.let { if (it.has("rows_affected")) it.getLong("rows_affected") else null }
        } finally { runCatching { request("v1/sessions/$session", "DELETE", token = token) } }
    }
    private fun token(j: JSONObject, username: String): AccessToken {
        val value = j.getString("access_token")
        require(value.isNotEmpty()) { "Server returned an empty access token." }
        return AccessToken(value, username, System.currentTimeMillis() + j.optLong("expires_in", 3600).coerceAtLeast(1) * 1000)
    }
}
fun localAddresses(): List<String> = NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
    .filter { it.isUp && !it.isLoopback }.flatMap { it.inetAddresses.toList() }
    .filterIsInstance<Inet4Address>().filter { !it.isLoopbackAddress }.mapNotNull { it.hostAddress }.distinct()
