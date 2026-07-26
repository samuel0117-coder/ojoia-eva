import http.server
import socketserver
import os

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # Servir archivos estáticos
        if self.path == '/':
            self.path = '/index.html'
        return super().do_GET()

    def log_message(self, format, *args):
        pass  # Silenciar logs

os.chdir('/home/sam/ojoia-eva/frontend')
with socketserver.TCPServer(('0.0.0.0', 8080), Handler) as httpd:
    print('Frontend en http://0.0.0.0:8080')
    httpd.serve_forever()
