"""A tiny stand-in for Ollama's embedding API, for tests and CI.

Embeddings are deterministic bag-of-words vectors: texts that share words get
similar vectors, unrelated texts get similarity close to 0.

    python tests/fake_ollama.py --port 11434
"""

from __future__ import annotations

import hashlib
import http.server
import json
import math
import re
import threading

STOP = set("""der die das und oder ein eine einer eines mit für fur von zu im in ist sind auf über uber
nur so es den dem des nach bei auch wie was wer the a an and or of to in is are on for with you do i
need run which""".split())
DIM = 512


def fake_embed(text: str):
    v = [0.0] * DIM
    for w in re.findall(r"\w+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        stem = w[:5]
        h = int(hashlib.md5(stem.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeOllama(http.server.BaseHTTPRequestHandler):
    mode = {"legacy_only": False, "model": "bge-m3", "requests": 0}

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": self.mode["model"] + ":latest"}]})
        else:
            self._send(404, {"error": "page not found"})

    def do_POST(self):
        FakeOllama.mode["requests"] += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("model") != self.mode["model"]:
            return self._send(404, {"error": f"model \"{body.get('model')}\" not found, try pulling it first"})
        if self.path == "/api/embed":
            if self.mode["legacy_only"]:
                return self._send(404, {"error": "404 page not found"})
            inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return self._send(200, {"embeddings": [fake_embed(t) for t in inp]})
        if self.path == "/api/embeddings":
            return self._send(200, {"embedding": fake_embed(body["prompt"])})
        self._send(404, {"error": "page not found"})

    def log_message(self, *a):
        pass




def start(port: int = 0) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11434)
    ap.add_argument("--model", default="bge-m3")
    a = ap.parse_args()
    FakeOllama.mode["model"] = a.model
    print(f"fake Ollama on http://127.0.0.1:{a.port} (model {a.model})", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), FakeOllama).serve_forever()
