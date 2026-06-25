#!/usr/bin/env python3
"""Proxy Limpio - Centro de Mando Audiovisual v3.0"""
import http.server, socketserver, json, urllib.request, urllib.error, subprocess, os

PORT = 8090
DIRECTORY = "/home/sam/ai_system"

ROUTES = [
    ("/api/sdxl/",      "http://127.0.0.1:8011/api/sdxl"),
    ("/api/flux/",       "http://127.0.0.1:8006/api/flux"),  # Flux via ComfyUI
    ("/api/comfyui/",   "http://127.0.0.1:8006"),
    ("/api/audioldm2/", "http://127.0.0.1:8009"),
    ("/api/voxtral/",   "http://127.0.0.1:8010"),
    ("/v1/audio/",      "http://127.0.0.1:8010"),
    ("/api/projects",   "http://127.0.0.1:8012"),
    ("/admin/gpu2/",    "http://127.0.0.1:8005"),
]

def resolve(path):
    for prefix, target in ROUTES:
        if path.startswith(prefix):
            rel = path[len(prefix):]
            if not rel.startswith("/"):
                rel = "/" + rel
            return target + rel
    return None

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","*")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/gpu2/status":
            self._gpu2_status()
            return
        target = resolve(self.path)
        if target:
            self._proxy("GET", target)
        elif p == "/movie":
            self.path = "/movie.html"
            super().do_GET()
        elif p == "/":
            self.path = "/dashboard.html"
            super().do_GET()
        else:
            try: super().do_GET()
            except: self.send_error(404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/gpu2/clear":
            self._gpu2_clear()
            return
        target = resolve(self.path)
        if target:
            self._proxy("POST", target)
        else:
            self.send_error(404)

    def _gpu2_status(self):
        try:
            mu = subprocess.check_output(["nvidia-smi","-i","2","--query-gpu=memory.used,memory.total","--format=csv,noheader,nounits"]).decode().strip().split(", ")
            self._send_json({"gpu":2,"memory_used_mb":int(mu[0]),"memory_total_mb":int(mu[1])})
        except Exception as e:
            self._send_json({"error":str(e)},500)

    def _gpu2_clear(self):
        try:
            subprocess.run(["pkill","-f","audioldm2_server"],capture_output=True,timeout=5)
            subprocess.run(["pkill","-STOP","-f","voxtral/src/serve"],capture_output=True,timeout=5)
            self._send_json({"status":"ok"})
        except Exception as e:
            self._send_json({"error":str(e)},500)

    def _proxy(self, method, target):
        try:
            body = None
            cl = int(self.headers.get("Content-Length",0))
            if method=="POST" and cl>0:
                body = self.rfile.read(cl)
            req = urllib.request.Request(target, data=body, method=method)
            for k,v in self.headers.items():
                if k.lower() in ("host","connection","transfer-encoding","content-length"): continue
                req.add_header(k,v)
            if body: req.add_header("Content-Length",str(len(body)))
            resp = urllib.request.urlopen(req, timeout=120)
            self.send_response(resp.status)
            for k,v in resp.getheaders():
                if k.lower() in ("transfer-encoding","connection"): continue
                self.send_header(k,v)
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            while True:
                chunk = resp.read(65536)
                if not chunk: break
                self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(json.dumps({"error":f"Backend HTTP {e.code}"}).encode())
        except Exception as e:
            try:
                self.send_response(502)
                self.send_header("Content-Type","application/json")
                self.send_header("Access-Control-Allow-Origin","*")
                self.end_headers()
                self.wfile.write(json.dumps({"error":str(e)[:100]}).encode())
            except: pass

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *a):
        pass

class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    with ReusableTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"[PROXY] http://0.0.0.0:{PORT}", flush=True)
        httpd.serve_forever()
