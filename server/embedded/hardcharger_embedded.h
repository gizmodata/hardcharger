/*
 * Hardcharger in an app's own process: the C API of the hardcharger_embedded library
 * (server/CMakeLists.txt, HARDCHARGER_EMBEDDED=ON), which the iOS app (ios/) links.
 *
 * The server is hardcharger-server itself, configured by the same command line (flags,
 * HARDCHARGER_* environment variables, a YAML file through --config): this API only
 * starts it on a thread of its own, says when it serves, and stops it. One server runs at
 * a time in a process; once it has stopped, another can start (a different configuration
 * included).
 *
 * Thread safety: every function may be called from any thread. Start blocks until the
 * server serves or has failed, and wait until it has stopped, so neither belongs on a
 * thread that must stay responsive (an app's main thread).
 */
#ifndef HARDCHARGER_EMBEDDED_H
#define HARDCHARGER_EMBEDDED_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* This header's version; changes when a function's meaning does. */
#define HARDCHARGER_EMBEDDED_API_VERSION 1

typedef struct hardcharger_server hardcharger_server;

/*
 * Starts a server configured by argv, as hardcharger-server's command line (argv[0] is
 * the program's name; the rest are its options: `--port 31337 --web-ui ...`). Returns 0
 * once it serves, with the server in *server. Otherwise returns nonzero, with *server
 * NULL and the reason in error (NUL-terminated, cut to error_size): the options were
 * wrong (2), the server could not start (1), or another server is running in this
 * process (3). The strings are copied; the caller may free them on return.
 */
int hardcharger_server_start(int argc, const char *const *argv, hardcharger_server **server, char *error,
                             size_t error_size);

/* The port the server listens on (the one it bound, when the configuration said 0). */
int hardcharger_server_port(const hardcharger_server *server);

/*
 * Asks the server to stop, and returns at once. immediately = 0: as hardcharger-server's
 * first SIGTERM, it drains (finishes running statements, refuses new work) for up to its
 * shutdown_grace_period_seconds, if graceful_shutdown is on; 1: it stops now, as on a
 * second signal. Safe to call more than once, and after the server stopped by itself.
 */
void hardcharger_server_request_stop(hardcharger_server *server, int immediately);

/* 1 while the server runs; 0 once it has stopped (asked to, or on an error). */
int hardcharger_server_is_running(const hardcharger_server *server);

/*
 * Waits until the server has stopped (asked to, or on an error) and returns its exit
 * status as hardcharger-server's would be (0: a clean stop); if it stopped on an error,
 * the reason goes to error as in hardcharger_server_start (error may be NULL). It may be
 * called again, and from a thread of its own (an app's watcher) while another thread asks
 * the server to stop.
 */
int hardcharger_server_wait(hardcharger_server *server, char *error, size_t error_size);

/*
 * Frees a server that has stopped (waits for it first if it hasn't), so another can
 * start. No call may use the server, or still be using it, once this is called.
 */
void hardcharger_server_free(hardcharger_server *server);

/* Versions, as hardcharger-server reports them. Static strings. */
const char *hardcharger_embedded_version(void);        /* Hardcharger: "2.0.0-rc.1" */
const char *hardcharger_embedded_duckdb_version(void); /* the DuckDB library */
const char *hardcharger_embedded_warp_sql_version(void); /* the Warp SQL SDK */
const char *hardcharger_embedded_web_ui_version(void); /* "" when no web UI is built in */

/* THIRD_PARTY_NOTICES.md as built in (hardcharger-server --licenses). Static. */
const char *hardcharger_embedded_third_party_notices(void);

#ifdef __cplusplus
}
#endif

#endif /* HARDCHARGER_EMBEDDED_H */
