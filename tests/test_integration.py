import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.name == "nt":
    os.environ["REMOTEPS_SHELL"] = "/bin/sh"  # fuerza el modo test POSIX
import servidor  # noqa: E402

BASE_TMP = None
URL = None


def setUpModule():
    global BASE_TMP, URL
    if not os.path.exists("/bin/sh"):
        raise unittest.SkipTest("este modulo requiere /bin/sh (Linux/macOS)")
    BASE_TMP = tempfile.mkdtemp(prefix="rts-int-")
    servidor._session["cwd"] = BASE_TMP
    httpd = servidor.serve("127.0.0.1", 0)
    URL = f"http://127.0.0.1:{httpd.server_address[1]}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    setUpModule.httpd = httpd


def tearDownModule():
    try:
        setUpModule.httpd.shutdown()
    finally:
        shutil.rmtree(BASE_TMP, ignore_errors=True)


def rpc(path, payload=None, raw=None, headers=None, timeout=60):
    data = raw if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None)
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(URL + path, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        ct = r.headers.get("Content-Type", "")
        return (json.loads(body.decode("utf-8")), r.status) if ct.startswith(
            "application/json") else (body, r.status)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        servidor._session["cwd"] = BASE_TMP

    def test_01_ping(self):
        r, code = rpc("/ping")
        self.assertEqual(code, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["cwd"], BASE_TMP)

    def test_02_execute_ok(self):
        r, _ = rpc("/execute", {"cmd": "echo integridad"})
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["stdout"].strip(), "integridad")

    def test_03_execute_comillas(self):
        r, _ = rpc("/execute", {"cmd": "printf \"a'b\\\"c\""})
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["stdout"], "a'b\"c")

    def test_04_execute_error(self):
        r, _ = rpc("/execute", {"cmd": "no-hay-nada-asi-xyz"})
        self.assertNotEqual(r["rc"], 0)
        self.assertTrue(r["stderr"])

    def test_05_cd_persistente(self):
        sub = os.path.join(BASE_TMP, "sub")
        os.mkdir(sub)
        r, _ = rpc("/execute", {"cmd": f"cd {sub}"})
        self.assertEqual(r["rc"], 0)
        r2, _ = rpc("/ping")
        self.assertEqual(os.path.realpath(r2["cwd"]), os.path.realpath(sub))

    def test_06_timeout(self):
        import time
        t0 = time.time()
        r, _ = rpc("/execute", {"cmd": "sleep 30", "timeout": 1}, timeout=30)
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(r["rc"], -1)
        self.assertIn("timeout", r["stderr"])

    def test_07_truncado(self):
        r, _ = rpc("/execute", {"cmd": "head -c 600000 /dev/zero | base64"},
                   timeout=60)
        self.assertTrue(r["truncated"])

    def test_08_ciclo_fs(self):
        r, _ = rpc("/fs/mkdir", {"path": "proyecto"})
        self.assertTrue(r["ok"])
        r, _ = rpc("/fs/write", {"path": "proyecto/notas.md",
                                 "content": "# acentos ñ é\nlinea2"})
        self.assertEqual(r["bytes"], len("# acentos ñ é\nlinea2".encode()))
        r, _ = rpc("/fs/read", {"path": "proyecto/notas.md"})
        self.assertEqual(r["content"], "# acentos ñ é\nlinea2")
        r, _ = rpc("/fs/list", {"path": "proyecto"})
        self.assertEqual([e["name"] for e in r["entries"]], ["notas.md"])
        blob = bytes(range(256)) * 500
        r, _ = rpc("/fs/upload", raw=blob, headers={
            "X-Remote-Path": urllib.parse.quote("proyecto/datos.bin")})
        self.assertEqual(r["bytes"], len(blob))
        got, _ = rpc("/fs/download", {"path": "proyecto/datos.bin"})
        self.assertEqual(got, blob)
        r, _ = rpc("/fs/delete", {"path": "proyecto", "recursive": True})
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(BASE_TMP, "proyecto")))

    def test_09_errores_http(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            rpc("/noexiste", {})
        self.assertEqual(cm.exception.code, 404)
        req = urllib.request.Request(URL + "/execute", data=b"no-json",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)

    def test_10_token_auth(self):
        servidor.AUTH_TOKEN = "secretito"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                rpc("/ping")
            self.assertEqual(cm.exception.code, 401)
            r, code = rpc("/ping", headers={"Authorization": "Bearer secretito"})
            self.assertEqual(code, 200)
            self.assertTrue(r["ok"])
        finally:
            servidor.AUTH_TOKEN = ""

    def test_11_delete_raiz_bloqueado(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            rpc("/fs/delete", {"path": "/", "recursive": True})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
