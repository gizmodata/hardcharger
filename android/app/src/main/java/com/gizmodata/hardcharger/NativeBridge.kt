package com.gizmodata.hardcharger

/** Blocking calls belong on Dispatchers.IO; cancellation may run concurrently. */
object NativeBridge {
    init { System.loadLibrary("hardcharger_android") }
    external fun version(): String
    external fun distribution(): String
    /** Runtime versions: Hardcharger server, DuckDB, Chargewire, embedded web UI. */
    external fun versionDetails(): Array<String>
    external fun notices(): String
    external fun start(arguments: Array<String>, password: String, secret: String, caBundle: String): Int
    external fun stop()
    external fun running(): Boolean
    external fun openConsole(host: String, port: Int, tls: Boolean, roots: String,
                             username: String, password: String, token: String)
    external fun runConsole(input: String): String
    external fun cancelConsole()
    external fun closeConsole()
    external fun complete(input: String): Boolean
    external fun makeCertificate(directory: String, names: String)
}
