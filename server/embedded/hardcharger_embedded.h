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
const char *hardcharger_embedded_chargewire_version(void); /* the Chargewire SDK */
const char *hardcharger_embedded_web_ui_version(void); /* "" when no web UI is built in */

/* THIRD_PARTY_NOTICES.md as built in, then the web UI's notices (hardcharger-server
 * --licenses). Static. */
const char *hardcharger_embedded_third_party_notices(void);

/*
 * The console: the `hardcharger` command-line client inside an app, for a SQL console
 * screen. The same statement splitting (a statement ends at a ';' outside strings, quoted
 * names, comments and dollar quotes), dot commands (.help, .tables, .schema, .mode, .timer,
 * .maxrows, ...) and output formatting as `hardcharger -c`, against any Chargewire server.
 * A console is used by one thread at a time, except hardcharger_console_cancel.
 */
typedef struct hardcharger_console hardcharger_console;

typedef struct hardcharger_console_options {
  const char *host;
  int port;
  int tls;                   /* 1: https */
  const char *tls_roots;     /* PEM file of the certificates to trust (a self-signed
                                server's own); NULL: the system's CA bundle */
  const char *username;      /* with password: the password grant */
  const char *password;
  const char *access_token;  /* instead: a token the app already has */
  int max_width;             /* columns a table may use; 0: no limit */
  int max_rows;              /* rows a result shows (.maxrows); 0: all of them */
} hardcharger_console_options;

/* Connects (signs in, opens a session). Returns 0, or nonzero with the reason in error. */
int hardcharger_console_open(const hardcharger_console_options *options, hardcharger_console **console,
                             char *error, size_t error_size);

/*
 * Runs `input` as `hardcharger -c` would: dot commands, then each complete statement in
 * order, stopping at the first error (with more than one statement, each is echoed before
 * its output, so the transcript says which one failed); a last statement without ';' runs
 * too. Returns what the client wrote (results, messages, errors), NUL-terminated; free it
 * with hardcharger_console_free_text. *exit_requested (may be NULL) is set to 1 after .exit
 * or .quit.
 */
char *hardcharger_console_run(hardcharger_console *console, const char *input, int *exit_requested);

/* Cancels the statement running in hardcharger_console_run (from another thread). */
void hardcharger_console_cancel(hardcharger_console *console);

/* The prompts: "catalog.schema> " and its continuation ("   -> "). Free with
 * hardcharger_console_free_text. */
char *hardcharger_console_prompt(hardcharger_console *console, int continuation);

/*
 * Whether `input` is ready to run as the client reads it: every statement in it ends with
 * a ';' (outside strings, quoted names, comments and dollar quotes), or it is a dot
 * command; 0 while a statement is still open (or there is nothing but whitespace and
 * comments). The statements it holds go to *statements (may be NULL).
 */
int hardcharger_console_input_complete(const char *input, int *statements);

void hardcharger_console_close(hardcharger_console *console);
void hardcharger_console_free_text(char *text);

#ifdef __cplusplus
}
#endif

#endif /* HARDCHARGER_EMBEDDED_H */
