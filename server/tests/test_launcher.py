"""The test harness itself: a server the launcher hands out is the one it started."""

import http.server
import threading

import pytest

from conftest import free_port


def test_the_launcher_refuses_an_answer_from_another_process(launcher):
    """Another process already answers on the port (as a server of another run or tool can on
    this machine): the server started there fails to bind and exits, and the launcher must say
    so rather than hand its tests the other process, whose owner may stop it mid-test."""

    class Impostor(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            pass

    port = free_port()
    impostor = http.server.ThreadingHTTPServer(server_address=("127.0.0.1", port), RequestHandlerClass=Impostor)
    thread = threading.Thread(target=impostor.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="answered, but by another process|server exited"):
            launcher.start(port=port)
    finally:
        impostor.shutdown()
        impostor.server_close()
