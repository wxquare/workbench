import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def upstream(body, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            requests.append((self.path, dict(self.headers), payload))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            result = body(payload) if callable(body) else body
            self.wfile.write(result if isinstance(result, bytes) else json.dumps(result).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/query", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
