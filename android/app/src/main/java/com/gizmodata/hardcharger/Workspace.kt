package com.gizmodata.hardcharger

import androidx.activity.compose.BackHandler
import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.webkit.*
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import java.security.MessageDigest
import java.security.cert.CertificateFactory

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun Workspace(model: AppModel, pickFiles: (ValueCallback<Array<Uri>>, Array<String>) -> Unit,
              export: (String, ByteArray) -> Unit, visible: Boolean = true) {
    val active = model.connection
    var failure by remember(model.webGeneration) { mutableStateOf<String?>(null) }
    var canGoBack by remember(model.webGeneration) { mutableStateOf(false) }
    var currentView by remember(model.webGeneration) { mutableStateOf<WebView?>(null) }
    BackHandler(enabled = visible && canGoBack) { currentView?.goBack() }
    var loading by remember(model.webGeneration) { mutableStateOf(true) }
    if (active == null) {
        if (!visible) return
        EmptyPanel("Your SQL workspace", "Start the on-device server or connect to a saved server to query data.")
        return
    }
    Column(Modifier.fillMaxSize().alpha(if (visible) 1f else 0f)) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(active.label, Modifier.weight(1f).padding(vertical = 12.dp), style = MaterialTheme.typography.labelLarge)
            TextButton(onClick = { model.launch { model.refreshConnection() } }, enabled = visible) { Text("Reconnect") }
        }
        failure?.let { Text(it, Modifier.padding(16.dp), color = MaterialTheme.colorScheme.error) }
        if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
        key(model.webGeneration) {
            var view: WebView? by remember { mutableStateOf(null) }
            DisposableEffect(Unit) {
                onDispose { view?.apply { stopLoading(); loadUrl("about:blank"); removeAllViews(); destroy() } }
            }
            AndroidView(modifier = Modifier.fillMaxSize(), update = { it.visibility = if (visible) android.view.View.VISIBLE else android.view.View.INVISIBLE }, factory = { context ->
                WebView(context).apply {
                    view = this; currentView = this
                    // WebView's default WRAP_CONTENT makes CSS vh/percentage heights zero
                    // inside AndroidView even when its visible viewport has a height.
                    layoutParams = android.view.ViewGroup.LayoutParams(
                        android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                        android.view.ViewGroup.LayoutParams.MATCH_PARENT)
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                    settings.setSupportMultipleWindows(false)
                    CookieManager.getInstance().setAcceptThirdPartyCookies(this, false)
                    WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
                    webViewClient = object : WebViewClient() {
                        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) { loading = true }
                        override fun onPageFinished(view: WebView, url: String?) { loading = false; canGoBack = view.canGoBack() }
                        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                            val uri = request.url
                            val same = sameOrigin(uri, active.endpoint.origin)
                            if (same) return false
                            if (request.isForMainFrame && uri.scheme in listOf("https", "http")) {
                                runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                            }
                            return true
                        }
                        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                            if (request.isForMainFrame) { failure = error.description.toString(); loading = false }
                        }
                        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                            // Only the selected endpoint's exact approved certificate. No expiry/name overrides.
                            val trusted = runCatching {
                                val allowed = active.endpoint.certificatePEM
                                require(allowed.isNotEmpty() && sameOrigin(Uri.parse(error.url), active.endpoint.origin))
                                require(!error.hasError(SslError.SSL_EXPIRED) && !error.hasError(SslError.SSL_NOTYETVALID) && !error.hasError(SslError.SSL_IDMISMATCH))
                                val der = android.net.http.SslCertificate.saveState(error.certificate).getByteArray("x509-certificate")!!
                                val cert = CertificateFactory.getInstance("X.509").generateCertificate(der.inputStream()) as java.security.cert.X509Certificate
                                cert.checkValidity()
                                MessageDigest.isEqual(cert.encoded, certificate(allowed).encoded)
                            }.getOrDefault(false)
                            if (trusted) handler.proceed() else { handler.cancel(); failure = "The server certificate could not be verified." }
                        }
                    }
                    webChromeClient = object : WebChromeClient() {
                        override fun onShowFileChooser(webView: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                            pickFiles(callback, params.acceptTypes.filter { it.isNotBlank() }.toTypedArray().ifEmpty { arrayOf("*/*") })
                            return true
                        }
                    }
                    if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                        failure = "Update Android System WebView to enable secure automatic sign-in."
                        loading = false
                    } else {
                        val origin = active.endpoint.origin
                        val payload = active.token.payload().toString()
                        val id = MessageDigest.getInstance("SHA-256").digest(active.token.token.toByteArray()).joinToString("") { "%02x".format(it) }
                        WebViewCompat.addDocumentStartJavaScript(this, """
                            (() => {
                              if (window !== window.top || location.origin !== ${JSONObject.quote(origin)}) return;
                              if (sessionStorage.getItem('hardcharger-android:token') !== '$id') {
                                sessionStorage.setItem('hardcharger-ui:sign-in', JSON.stringify($payload));
                                sessionStorage.setItem('hardcharger-ui:user-name', ${JSONObject.quote(active.token.username)});
                                sessionStorage.setItem('hardcharger-android:token', '$id');
                              }
                            })();
                        """.trimIndent(), setOf(origin))
                        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
                            WebViewCompat.addWebMessageListener(this, "HardchargerExport", setOf(origin)) { _, message, source, mainFrame, _ ->
                                if (mainFrame && sameOrigin(source, origin)) {
                                    runCatching {
                                        val raw = message.data ?: "{}"
                                        require(raw.length <= 70 * 1024 * 1024) { "Export is too large." }
                                        val j = JSONObject(raw)
                                        val bytes = android.util.Base64.decode(j.getString("data"), android.util.Base64.DEFAULT)
                                        require(bytes.size <= 50 * 1024 * 1024) { "Export exceeds 50 MB. Use the SQL console to export larger files." }
                                        export(j.optString("name", "export.csv"), bytes)
                                    }.onFailure { model.error = it.message }
                                }
                            }
                            WebViewCompat.addDocumentStartJavaScript(this, """
                                // Keep the actual Blob: the workspace's CSP intentionally
                                // excludes blob: from connect-src, so fetch(blobURL) is refused.
                                const exportBlobs = new Map();
                                const createObjectURL = URL.createObjectURL.bind(URL);
                                const revokeObjectURL = URL.revokeObjectURL.bind(URL);
                                URL.createObjectURL = (blob) => {
                                  const url = createObjectURL(blob); exportBlobs.set(url, blob); return url;
                                };
                                URL.revokeObjectURL = (url) => {
                                  exportBlobs.delete(url); revokeObjectURL(url);
                                };
                                document.addEventListener('click', async (event) => {
                                  const a = event.target.closest?.('a[download]');
                                  if (!a || !/^(blob:|data:)/.test(a.href)) return;
                                  event.preventDefault(); event.stopImmediatePropagation();
                                  try {
                                    let blob = exportBlobs.get(a.href);
                                    if (!blob && a.href.startsWith('data:')) {
                                      const [header, encoded] = a.href.split(',', 2);
                                      const raw = header.includes(';base64') ? atob(encoded) : decodeURIComponent(encoded);
                                      blob = new Blob([Uint8Array.from(raw, c => c.charCodeAt(0))]);
                                    }
                                    if (!blob) throw new Error('This download is no longer available. Run the query and try again.');
                                    if (blob.size > 50 * 1024 * 1024) { alert('Export exceeds 50 MB. Export larger files from the SQL console.'); return; }
                                    const reader = new FileReader();
                                    reader.onload = () => HardchargerExport.postMessage(JSON.stringify({name: a.download || 'export.csv', data: reader.result.split(',')[1]}));
                                    reader.readAsDataURL(blob);
                                  } catch (e) { alert('Could not export: ' + e.message); }
                                }, true);
                            """.trimIndent(), setOf(origin))
                        }
                        loadUrl("$origin/ui/")
                    }
                }
            })
        }
    }
}
fun sameOrigin(uri: Uri, origin: String): Boolean {
    val expected = Uri.parse(origin)
    fun port(u: Uri) = if (u.port != -1) u.port else if (u.scheme == "https") 443 else 80
    return uri.scheme == expected.scheme && uri.host == expected.host && port(uri) == port(expected)
}
