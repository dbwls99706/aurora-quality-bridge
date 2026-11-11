"""Exercise real GET response headers without sockets, including bad Windows MIME registry."""
import io
import mimetypes
import sys
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import Handler


class StaticMimeTests(unittest.TestCase):
    def response(self, path):
        handler = object.__new__(Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers['Host'] = '127.0.0.1:8000'
        handler.server = SimpleNamespace(allowed_authorities={'127.0.0.1:8000'}, cloud_policy=SimpleNamespace(enabled=False))
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        handler.do_GET()
        return handler.send_response.call_args.args[0], dict(call.args for call in handler.send_header.call_args_list), handler.wfile.getvalue()

    def test_javascript_ignores_windows_plain_text_association(self):
        with patch.dict(mimetypes.types_map, {'.js': 'text/plain'}), patch('mimetypes.guess_type', return_value=('text/plain', None)) as guess:
            for name in ('app.js', 'landing.js', 'common-nav.js', 'supply-alternatives.js'):
                with self.subTest(name=name):
                    status, headers, data = self.response('/static/' + name)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['Content-Type'], 'text/javascript; charset=utf-8')
                    self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                    self.assertEqual(int(headers['Content-Length']), len(data))
            guess.assert_not_called()

    def test_css_html_svg_are_portable(self):
        for path, mime in (('/static/workspace.css', 'text/css; charset=utf-8'), ('/', 'text/html; charset=utf-8'), ('/workspace', 'text/html; charset=utf-8'), ('/static/favicon.svg', 'image/svg+xml')):
            with self.subTest(path=path), patch('mimetypes.guess_type', return_value=(None, None)):
                status, headers, data = self.response(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertTrue(data)

    def test_non_ui_files_still_rejected(self):
        for path in ('/backend.py', '/static/../backend.py', '/static/missing.js'):
            with self.subTest(path=path):
                status, headers, _ = self.response(path)
                self.assertEqual(status, 404)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')


if __name__ == '__main__':
    unittest.main()
