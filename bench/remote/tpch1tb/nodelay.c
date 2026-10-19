/* Diagnostics only (servers.sh SERVER_LD_PRELOAD): sets TCP_NODELAY on every accepted socket.
 * gcc -shared -fPIC -O2 -o nodelay.so nodelay.c -ldl */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
static void nd(int fd) { if (fd >= 0) { int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one); } }
int accept(int s, struct sockaddr *a, socklen_t *l) { static int (*r)(int, struct sockaddr *, socklen_t *); if (!r) r = dlsym(RTLD_NEXT, "accept"); int fd = r(s, a, l); nd(fd); return fd; }
int accept4(int s, struct sockaddr *a, socklen_t *l, int f) { static int (*r)(int, struct sockaddr *, socklen_t *, int); if (!r) r = dlsym(RTLD_NEXT, "accept4"); int fd = r(s, a, l, f); nd(fd); return fd; }
