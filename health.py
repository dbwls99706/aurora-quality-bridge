from http.server import HTTPServer,BaseHTTPRequestHandler
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  body=b'{"ok":true,"mode":"provision_only"}' if self.path=='/healthz' else b'Not available during validation.'
  self.send_response(200 if self.path=='/healthz' else 503)
  self.send_header('Content-Type','text/plain');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
HTTPServer(('0.0.0.0',8000),H).serve_forever()
