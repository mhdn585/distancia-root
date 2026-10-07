import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cliente


class TestParseInput(unittest.TestCase):
    def test_comando_remoto_crudo(self):
        self.assertEqual(cliente.parse_input("Get-Process | Where CPU -gt 1"),
                         ("remote", "Get-Process | Where CPU -gt 1", None))

    def test_comando_local(self):
        self.assertEqual(cliente.parse_input("/cd C:\\Users"),
                         ("local", "cd", "C:\\Users"))

    def test_comando_local_sin_args(self):
        self.assertEqual(cliente.parse_input("/ping"), ("local", "ping", ""))

    def test_comando_local_uppercase(self):
        self.assertEqual(cliente.parse_input("/PING"), ("local", "ping", ""))

    def test_salir_sin_barra(self):
        self.assertEqual(cliente.parse_input("salir"), ("local", "salir", ""))

    def test_linea_vacia(self):
        self.assertIsNone(cliente.parse_input("   "))

    def test_conserva_el_texto_remote(self):
        kind, cmd, _ = cliente.parse_input('echo "hola  mundo"')
        self.assertEqual(kind, "remote")
        self.assertEqual(cmd, 'echo "hola  mundo"')


class TestHelpersPuros(unittest.TestCase):
    def test_join_url(self):
        self.assertEqual(
            cliente.join_url("https://x.trycloudflare.com/", "/execute"),
            "https://x.trycloudflare.com/execute")
        self.assertEqual(
            cliente.join_url("https://x.com", "fs/list"),
            "https://x.com/fs/list")

    def test_human_size(self):
        self.assertEqual(cliente.human_size(500), "500 B")
        self.assertEqual(cliente.human_size(2048), "2.0 KB")
        self.assertEqual(cliente.human_size(5 * 1024 * 1024), "5.0 MB")

    def test_shorten(self):
        self.assertEqual(cliente.shorten("abcdef", 100), "abcdef")
        self.assertEqual(cliente.shorten("abcdefghij", 6), "...hij")
        self.assertEqual(cliente.shorten("", 10), "?")

    def test_remote_pjoin(self):
        self.assertEqual(cliente.remote_pjoin("C:\\Users\\x", "a.txt"),
                         "C:\\Users\\x\\a.txt")
        self.assertEqual(cliente.remote_pjoin("/home/u", "a.txt"),
                         "/home/u/a.txt")

    def test_local_basename(self):
        self.assertEqual(cliente.local_basename("C:\\a\\b.txt"), "b.txt")
        self.assertEqual(cliente.local_basename("/x/y/z"), "z")


class TestFormatResult(unittest.TestCase):
    def test_ok_silencioso(self):
        parts = cliente.format_result({"stdout": "hola", "stderr": "", "rc": 0,
                                       "truncated": False})
        self.assertEqual(parts, [("hola", "")])

    def test_rc_distinto_se_muestra(self):
        parts = cliente.format_result({"stdout": "", "stderr": "boom", "rc": 5,
                                       "truncated": False})
        texts = [t for t, _ in parts]
        self.assertIn("boom", texts)
        self.assertIn("[rc=5]", texts)
        self.assertIn("31;1", [c for _, c in parts])

    def test_truncado_avisa(self):
        parts = cliente.format_result({"stdout": "x", "stderr": "", "rc": 0,
                                       "truncated": True})
        self.assertTrue(any("truncada" in t for t, _ in parts))


class FakeResp:
    def __init__(self, body, ctype="application/json"):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRemoteTransporte(unittest.TestCase):
    def setUp(self):
        self.rm = cliente.Remote("https://x.test", cmd_timeout=5)

    def test_json_response(self):
        payload = json.dumps({"ok": True}).encode()
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(payload)):
            r = self.rm._req("GET", "/ping")
        self.assertEqual(r, {"ok": True})
        self.assertTrue(self.rm.ok)

    def test_bytes_response(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(b"\x00\x01", "application/octet-stream")):
            r = self.rm._req("POST", "/fs/download", {"path": "a"})
        self.assertEqual(r, b"\x00\x01")

    def test_url_error_mensaje_tunel(self):
        err = urllib.error.URLError("Connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(cliente.RemoteError) as cm:
                self.rm.ping()
        self.assertIn("sin conexion", str(cm.exception))
        self.assertFalse(self.rm.ok)

    def test_http_error_json(self):
        body = io.BytesIO(json.dumps({"error": "ruta desconocida"}).encode())
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, body)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(cliente.RemoteError) as cm:
                self.rm.fs_list()
        self.assertEqual(str(cm.exception), "ruta desconocida")

    def test_execute_actualiza_estado(self):
        payload = json.dumps({"stdout": "x", "stderr": "", "rc": 0,
                              "cwd": "C:\\t", "elapsed": 0.5,
                              "truncated": False}).encode()
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(payload)):
            self.rm.execute("echo x")
        self.assertEqual(self.rm.cwd, "C:\\t")
        self.assertEqual(self.rm.last, (0, 0.5))

    def test_token_bearer_viaja_en_header(self):
        rm = cliente.Remote("https://x.test", token="s3cr3t0")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            return FakeResp(json.dumps({"ok": True}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rm.ping()
        self.assertEqual(captured["headers"].get("Authorization"),
                         "Bearer s3cr3t0")

    def test_sin_token_no_envia_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            return FakeResp(json.dumps({"ok": True}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.rm.ping()
        self.assertNotIn("Authorization", {k.lower(): v for k, v in
                                           captured["headers"].items()})

    def test_upload_envia_header_ruta(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["data"] = req.data
            return FakeResp(json.dumps({"ok": True, "bytes": 3,
                                        "path": "/r/a.bin"}).encode())

        import tempfile
        fd, lp = tempfile.mkstemp()
        os.write(fd, b"abc")
        os.close(fd)
        try:
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                r = self.rm.upload(lp, "/r/a.bin")
            self.assertEqual(captured["data"], b"abc")
            self.assertEqual(r["ok"], True)
        finally:
            os.unlink(lp)


class TestToolbar(unittest.TestCase):
    def test_devuelve_tokens(self):
        rm = cliente.Remote("https://x.test")
        rm.host = "PC-TRABAJO"
        toks = cliente.toolbar(rm)
        self.assertTrue(any("PC-TRABAJO" in t for _, t in toks))


if __name__ == "__main__":
    unittest.main()
