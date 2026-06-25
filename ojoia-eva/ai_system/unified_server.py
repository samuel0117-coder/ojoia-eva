#!/usr/bin/env python3
"""Servidor unido: frontend estático + proxy API, mismo origen = sin CORS"""
import http.server
import urllib.request
import urllib.error
import os
import mimetypes

FRONTEND_DIR = "/home/sam/ojoia-pwa-recuperada"
BACKEND = "http://localhost:8005"

class UnifiedHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/") or self.path.startswith("/frames/") or self.path.startswith("/grid/") or self.path.startswith("/config/") or self.path.startswith("/admin/") or self.path.startswith("/debug/") or self.path.startswith("/devices/"):
            self._proxy()
        else:
            self._serve_static()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _proxy(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None
            # Mapear /api/xxx -> /api/xxx (igual)
            backend_url = BACKEND + self.path
            req = urllib.request.Request(backend_url, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() not in ("host", "connection"):
                    req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            import json
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_static(self):
        path = self.path
        if path == "/":
            path = "/index.html"
        filepath = os.path.join(FRONTEND_DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            content_type, _ = mimetypes.guess_type(filepath)
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            # SPA fallback
            index_path = os.path.join(FRONTEND_DIR, "index.html")
            if os.path.isfile(index_path):
                with open(index_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", 8007), UnifiedHandler)
    print("Unified server running on :8007 (frontend + API proxy)")
    server.serve_forever()
