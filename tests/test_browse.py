"""Tests for tools/browse.py. Run from the repository root: python3 -m unittest discover tests"""
import contextlib
import csv
import http.server
import importlib.util
import io
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('browse', os.path.join(ROOT, 'tools', 'browse.py'))
browse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browse)


class Results(unittest.TestCase):
    @unittest.skipUnless(shutil.which('openssl'), 'needs openssl to make a self-signed certificate')
    def test_self_signed_certificate_is_intercepted(self):
        """Like the ISP router's own page while the line is down (seen 05/10 at 11:59)."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        cert, key = os.path.join(tmp, 'cert.pem'), os.path.join(tmp, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj',
                        '/CN=router.local', '-keyout', key, '-out', cert], check=True, capture_output=True)
        server = http.server.HTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        row = browse.fetch(f'https://127.0.0.1:{server.server_port}/', 'page', 5, 1000)
        self.assertEqual(row['result'], 'intercepted')
        self.assertEqual(row['address'], '127.0.0.1')
        self.assertIn('certificate verify failed', row['error'])

    def test_refused_connection_is_failed(self):
        with socket.socket() as s:  # find a free port, then close it so nothing listens there
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        row = browse.fetch(f'https://127.0.0.1:{port}/', 'page', 5, 1000)
        self.assertEqual(row['result'], 'failed')

    def test_unknown_name_is_dns_failed(self):
        row = browse.fetch('https://no-such-host.invalid/', 'page', 5, 1000)
        self.assertEqual(row['result'], 'dns_failed')
        self.assertEqual(row['address'], '')


class Summary(unittest.TestCase):
    def write(self, rows):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'browse.csv')
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=browse.FIELDS)
            w.writeheader()
            for t, result, address in rows:
                w.writerow({'time': f'2026-10-05T{t}+02:00', 'kind': 'page', 'url': 'https://example.org/',
                            'result': result, 'address': address, 'first_byte_ms': 100 if result == 'ok' else ''})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            browse.summary(path)
        return out.getvalue()

    def test_two_outages_are_two_incidents(self):
        # 05/10: outages 11:59:27-12:00:21 and 12:01:45-12:02:08, a working request in between
        text = self.write([('11:59:30', 'ok', '198.51.100.1'),
                           ('11:59:47', 'intercepted', '192.168.1.1'),
                           ('11:59:53', 'intercepted', '192.168.1.1'),
                           ('12:00:13', 'intercepted', '192.168.1.1'),
                           ('12:00:40', 'ok', '198.51.100.1'),
                           ('12:01:50', 'intercepted', '192.168.1.1'),
                           ('12:02:07', 'intercepted', '192.168.1.1')])
        lines = [l for l in text.splitlines() if 'failures' in l]
        self.assertEqual(len(lines), 2, text)
        self.assertIn('11:59:47 - 12:00:13: 3 requests (intercepted); answered by local address 192.168.1.1', lines[0])
        self.assertIn('12:01:50 - 12:02:07: 2 requests', lines[1])

    def test_long_gap_splits_incidents(self):
        text = self.write([('10:00:00', 'timeout', ''), ('10:05:00', 'timeout', '')])
        self.assertEqual(sum('failures' in l for l in text.splitlines()), 2)


class OldLogFormat(unittest.TestCase):
    def test_new_rows_never_go_under_an_old_header(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        old = os.path.join(tmp, 'browse.csv')
        with open(old, 'w') as f:
            f.write('time,kind,url,result,http_status,dns_ms,first_byte_ms,total_ms,bytes,mbit_s,error\n')
        args = types.SimpleNamespace(out=old, duration=0, min_wait=1, max_wait=1, download_every=300,
                                     download_bytes=1000, page_bytes=1000, timeout=5, verbose=False)
        with contextlib.redirect_stdout(io.StringIO()):
            browse.run(args)
        with open(old) as f:
            self.assertNotIn('address', f.readline())  # the old file is left as it was
        new = [n for n in os.listdir(tmp) if n != 'browse.csv']
        self.assertEqual(len(new), 1)
        with open(os.path.join(tmp, new[0])) as f:
            self.assertEqual(f.readline().strip().split(','), browse.FIELDS)


if __name__ == '__main__':
    unittest.main()
