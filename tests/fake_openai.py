"""Local OpenAI-compatible HTTP server; never contacts a model provider."""
import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from iris.models import ModelConfig


def completion(content="{}", **extra):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}, **extra}


class FakeOpenAI:
    def __init__(self):
        self.responses = deque()
        self.requests = []
        self.lock = threading.Lock()
        self.handler = None

    def enqueue(self, status=200, body=None, headers=None, delay=0):
        self.responses.append((status, completion() if body is None else body, headers or {}, delay))

    @contextmanager
    def serve(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.lock:
                    owner.requests.append((self.path, payload))
                    scripted = owner.responses.popleft() if owner.responses else None
                if owner.handler:
                    scripted = owner.handler(self.path, payload)
                default = {"data": [{"embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 2}}
                status, body, headers, delay = scripted or (200, default if self.path.endswith("embeddings") else completion(), {}, 0)
                time.sleep(delay)
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.configs = {kind: ModelConfig(f"http://127.0.0.1:{server.server_port}/v1", "fake-only", "stub-" + kind)
                        for kind in ("chat", "embedding")}
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class Clock:
    def __init__(self):
        from datetime import datetime, timezone
        self.value = datetime(2026, 10, 4, 15, 59, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        from datetime import timedelta
        self.value += timedelta(seconds=seconds)
        self.elapsed += seconds

    def monotonic(self):
        return self.elapsed
