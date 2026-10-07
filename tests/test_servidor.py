import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("REMOTEPS_SHELL", "/bin/sh")

import servidor


class TestBuildCommand(unittest.TestCase):
    def setUp(self):
        self._ps = servidor.IS_POWERSHELL
        self._shell = servidor.SHELL

    def tearDown(self):
        servidor.IS_POWERSHELL = self._ps
        servidor.SHELL = self._shell

    def test_posix_build(self):
        argv = servidor.build_command("echo hola")
        self.assertEqual(argv[0], servidor.SHELL)
        self.assertEqual(argv[1], "-c")
        self.assertIn("echo hola", argv[2])
        self.assertIn(servidor.SENTINEL, argv[2])

    def test_powershell_encoded(self):
        import base64
        servidor.IS_POWERSHELL = True
        servidor.SHELL = "powershell.exe"
        argv = servidor.build_command("Get-Process | Select-Object Name")
        self.assertEqual(argv[:4], [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-EncodedCommand"])
        script = base64.b64decode(argv[4]).decode("utf-16-le")
        self.assertIn("Get-Process | Select-Object Name", script)
        self.assertIn("[Console]::OutputEncoding", script)
        self.assertIn(servidor.SENTINEL, script)
        self.assertIn("$LASTEXITCODE", script)

    def test_wrap_multiline_preserved(self):
        script = servidor.wrap_script("a\nb\nc")
        self.assertIn("a\nb\nc", script)


class TestParseMeta(unittest.TestCase):
    def test_with_sentinel(self):
        text = "salida1\nsalida2\n__REMOTEPS_META__|C:\\Users|0"
        clean, cwd, rc = servidor.parse_meta(text)
        self.assertEqual(clean, "salida1\nsalida2")
        self.assertEqual(cwd, "C:\\Users")
        self.assertEqual(rc, 0)

    def test_cwd_con_barra(self):
        text = "__REMOTEPS_META__|/a|b|7"
        clean, cwd, rc = servidor.parse_meta(text)
        self.assertEqual(cwd, "/a|b")
        self.assertEqual(rc, 7)
        self.assertEqual(clean, "")

    def test_sin_sentinel(self):
        clean, cwd, rc = servidor.parse_meta("hola")
        self.assertEqual((clean, cwd, rc), ("hola", None, None))


class TestCap(unittest.TestCase):
    def test_sin_truncar(self):
        data, tr = servidor._cap(b"x" * 100)
        self.assertEqual((tr, len(data)), (False, 100))

    def test_trunca_y_conserva_fin(self):
        data = b"A" * 400000 + b"B" * 400000
        capped, tr = servidor._cap(data)
        self.assertTrue(tr)
        self.assertTrue(capped.startswith(b"A"))
        self.assertTrue(capped.endswith(b"B"))
        self.assertIn(b"TRUNCADO", capped)
        self.assertLess(len(capped), servidor.MAX_OUTPUT + 200)


class TestPaths(unittest.TestCase):
    def test_relativo_al_session_cwd(self):
        orig = servidor._session["cwd"]
        try:
            servidor._session["cwd"] = "/base"
            self.assertEqual(servidor.expand_path("sub/x.txt"),
                             os.path.normpath("/base/sub/x.txt"))
            self.assertEqual(servidor.expand_path("/abs/y"),
                             os.path.normpath("/abs/y"))
        finally:
            servidor._session["cwd"] = orig

    def test_ruta_con_nulo(self):
        with self.assertRaises(ValueError):
            servidor.expand_path("a\x00b")


class TestFsFunctions(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = servidor._session["cwd"]
        self.tmp = tempfile.mkdtemp(prefix="rts-uni-")
        servidor._session["cwd"] = self.tmp

    def tearDown(self):
        servidor._session["cwd"] = self._orig_cwd
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_roundtrip(self):
        r = servidor.fs_write("hola.txt", "línea 1\nlínea 2 ñ")
        self.assertTrue(r["ok"])
        data = servidor.fs_read("hola.txt")
        self.assertEqual(data["content"], "línea 1\nlínea 2 ñ")
        self.assertFalse(data["truncated"])

    def test_list_ordena_dirs_primero(self):
        os.mkdir(os.path.join(self.tmp, "zz_dir"))
        servidor.fs_write("aa.txt", "x")
        r = servidor.fs_list("")
        names = [e["name"] for e in r["entries"]]
        self.assertEqual(names, ["zz_dir", "aa.txt"])
        self.assertTrue(r["entries"][0]["dir"])

    def test_mkdir_delete(self):
        servidor.fs_mkdir("a/b/c")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "a/b/c")))
        servidor.fs_delete("a", recursive=True)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a")))

    def test_delete_raiz_bloqueado(self):
        with self.assertRaises(ValueError):
            servidor.fs_delete(os.path.abspath(os.sep))

    def test_read_inexistente(self):
        with self.assertRaises(FileNotFoundError):
            servidor.fs_read("no-existe.txt")


class TestRunCommand(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = servidor._session["cwd"]
        self.tmp = tempfile.mkdtemp(prefix="rts-run-")
        servidor._session["cwd"] = self.tmp

    def tearDown(self):
        servidor._session["cwd"] = self._orig_cwd
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_echo_rc0(self):
        r = servidor.run_command("echo hola", 10)
        self.assertEqual(r["rc"], 0)
        self.assertIn("hola", r["stdout"])
        self.assertEqual(r["stderr"], "")

    def test_stderr_y_rc_distinto(self):
        r = servidor.run_command("echo mal 1>&2; exit 3", 10)
        self.assertEqual(r["rc"], 3)
        self.assertIn("mal", r["stderr"])

    def test_comando_inexistente_rc_nozero(self):
        r = servidor.run_command("definitivamente-no-existe-xyz", 10)
        self.assertNotEqual(r["rc"], 0)

    def test_actualiza_cwd_persistente(self):
        sub = os.path.join(self.tmp, "sub")
        os.mkdir(sub)
        r = servidor.run_command(f"cd {sub}", 10)
        self.assertEqual(r["rc"], 0)
        self.assertEqual(servidor._session["cwd"], os.path.realpath(sub))

    def test_timeout_mata(self):
        import time as _t
        t0 = _t.time()
        r = servidor.run_command("sleep 30", 1)
        self.assertLess(_t.time() - t0, 6)
        self.assertEqual(r["rc"], -1)
        self.assertIn("timeout", r["stderr"])

    def test_salida_grande_truncada(self):
        r = servidor.run_command("head -c 600000 /dev/zero | base64", 20)
        self.assertTrue(r["truncated"])
        self.assertLess(len(r["stdout"]), servidor.MAX_OUTPUT + 200)


class TestTunel(unittest.TestCase):
    """Funciones puras del modo --tunnel: sin red, sin cloudflared real."""

    def test_extract_url_linea_banner_real(self):
        linea = ("2026-10-07T19:18:38Z INF |  "
                 "https://already-gloves-balloon-brothers.trycloudflare.com")
        self.assertEqual(
            servidor.extract_tunnel_url(linea),
            "https://already-gloves-balloon-brothers.trycloudflare.com")

    def test_extract_url_en_texto_y_sin_barra_final(self):
        got = servidor.extract_tunnel_url(
            "Visitá https://postposted-fixes-mall-burns.trycloudflare.com/ hoy")
        self.assertEqual(got,
                         "https://postposted-fixes-mall-burns.trycloudflare.com")

    def test_extract_url_lineas_sin_trycloudflare(self):
        for linea in ("", None, "hola mundo",
                      "https://example.com/pagina",
                      "INF Registered tunnel connection ip=198.41.200.63"):
            self.assertIsNone(servidor.extract_tunnel_url(linea))

    def test_find_cloudflared_explicito(self):
        self.assertEqual(servidor.find_cloudflared(__file__), __file__)
        self.assertIsNone(servidor.find_cloudflared("/no/existe/cloudflared"))

    def test_find_cloudflared_busca_en_path(self):
        from unittest import mock
        with mock.patch.object(servidor.shutil, "which",
                               return_value="/usr/local/bin/cloudflared"):
            self.assertEqual(servidor.find_cloudflared(),
                             "/usr/local/bin/cloudflared")

    def test_tunnel_stop_sin_proceso_no_explode(self):
        t = servidor.Tunnel("definitivamente-no-existe", 1)
        t.stop()

    @unittest.skipIf(os.name == "nt", "el fake usa /bin/sh")
    def test_tunnel_extrae_url_de_hijo_fake(self):
        tmp = tempfile.mkdtemp(prefix="rts-tun-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, "fake-cf.sh")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    "echo 'xxx INF |  https://fake-a-b-c.trycloudflare.com'\n")
        os.chmod(fake, 0o755)
        t = servidor.Tunnel(fake, 9, quiet=True)
        t.start()
        self.assertTrue(t.url_event.wait(5))
        self.assertEqual(t.url, "https://fake-a-b-c.trycloudflare.com")
        t.stop()


if __name__ == "__main__":
    unittest.main()
