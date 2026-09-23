import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"queue": 0.0, "kv": 0.2}
LOCK = threading.Lock()

def burn(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    value = 0.0
    while time.monotonic() < deadline:
        for number in range(1, 10000): value += math.sqrt(number)
    assert value >= 0

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type="text/plain"):
        data = body.encode(); self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
        if parsed.path == "/health": return self.reply(200, "ok\n")
        if parsed.path == "/metrics":
            with LOCK: state = dict(STATE)
            return self.reply(200, "# TYPE vllm:num_requests_waiting gauge\n" + f"vllm:num_requests_waiting {state['queue']}\n" + "# TYPE vllm:kv_cache_usage_perc gauge\n" + f"vllm:kv_cache_usage_perc {state['kv']}\n")
        if parsed.path == "/control":
            with LOCK:
                for key in ("queue", "kv"):
                    if key in query: STATE[key] = float(query[key][0])
                state = dict(STATE)
            return self.reply(200, json.dumps(state) + "\n", "application/json")
        if parsed.path == "/burn":
            seconds = float(query.get("seconds", ["90"])[0]); threading.Thread(target=burn, args=(seconds,), daemon=True).start(); return self.reply(202, "{}\n", "application/json")
        self.reply(404, "not found\n")
    def log_message(self, *_): pass

ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
