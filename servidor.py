#!/usr/bin/env python3
"""servidor.py — RemotePS | PC objetivo (trabajo).

Escucha SOLO en 127.0.0.1:8080. Recibe comandos por HTTP (reenviados por
cloudflared), los ejecuta en PowerShell y devuelve stdout/stderr/rc en JSON.
Solo usa la libreria estandar de Python (no requiere pip install).

Configuracion por variables de entorno:
  REMOTEPS_PORT    puerto local (default 8080)
  REMOTEPS_TOKEN   token Bearer requerido en cada request (vacio = sin auth)
  REMOTEPS_SHELL   shell a usar (default powershell.exe; /bin/sh se usa en tests)

Modo terminal unico:
  python servidor.py --tunnel
  Lanza cloudflared como proceso hijo, extrae la URL publica de su salida y la
  imprime en esta misma terminal. Ctrl+C apaga servidor + tunel a la vez.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HOST = "127.0.0.1"
PORT = int(os.environ.get("REMOTEPS_PORT", "8080"))
AUTH_TOKEN = os.environ.get("REMOTEPS_TOKEN", "")

MAX_OUTPUT = 512 * 1024
MAX_READ_FILE = 2 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
SENTINEL = "__REMOTEPS_META__"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

SHELL = os.environ.get("REMOTEPS_SHELL") or (
    "powershell.exe" if os.name == "nt" else "/bin/sh"
)
IS_POWERSHELL = os.path.basename(SHELL).lower().startswith("powershell")

_lock = threading.Lock()
_session = {"cwd": os.path.expanduser("~")}
_psver = None


class BadReq(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def check_auth(headers) -> bool:
    if not AUTH_TOKEN:
        return True
    return headers.get("Authorization", "") == f"Bearer {AUTH_TOKEN}"


def wrap_script(cmd: str) -> str:
    if IS_POWERSHELL:
        header = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "$OutputEncoding=[Text.Encoding]::UTF8; "
        )
        tail = (
            "\n$__ok = $?\n"
            "$__rc = 0\n"
            "if ($null -ne $LASTEXITCODE) { $__rc = [int]$LASTEXITCODE }\n"
            "if ($__rc -eq 0 -and -not $__ok) { $__rc = 1 }\n"
            "Write-Output ''\n"
            "Write-Output ('" + SENTINEL + "|' + (Get-Location).Path + '|' + $__rc)\n"
            "exit $__rc\n"
        )
        return header + "\n" + cmd + tail
    tail = (
        "\n__remoteps_rc=$?\n"
        "printf '\\n" + SENTINEL + "|%s|%s\\n' \"$PWD\" \"$__remoteps_rc\"\n"
        "exit $__remoteps_rc\n"
    )
    return cmd.rstrip("\n") + tail


def build_command(cmd: str) -> list:
    script = wrap_script(cmd)
    if IS_POWERSHELL:
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [SHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
    return [SHELL, "-c", script]


def parse_meta(text: str):
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if line.startswith(SENTINEL + "|"):
            parts = line.split("|")
            try:
                rc = int(parts[-1])
            except (ValueError, IndexError):
                rc = None
            cwd = "|".join(parts[1:-1])
            del lines[i]
            return "\n".join(lines).rstrip("\r\n"), cwd, rc
    return text, None, None


def _cap(b: bytes):
    if len(b) <= MAX_OUTPUT:
        return b, False
    half = MAX_OUTPUT // 2
    mark = b"\n\n...[TRUNCADO: la salida supera el limite]...\n\n"
    return b[:half] + mark + b[-half:], True


def _decode(b) -> str:
    return b.decode("utf-8", errors="replace") if b else ""


def _kill(proc):
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
            return
        except OSError:
            pass
    else:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _popen(argv, cwd):
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": cwd}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def run_command(cmd: str, timeout: float) -> dict:
    with _lock:
        cwd = _session["cwd"]
    argv = build_command(cmd)
    t0 = time.time()
    try:
        proc = _popen(argv, cwd)
    except OSError:
        with _lock:
            _session["cwd"] = os.path.expanduser("~")
            cwd = _session["cwd"]
        try:
            proc = _popen(argv, cwd)
        except OSError as e2:
            return {"stdout": "", "stderr": f"no se pudo lanzar {SHELL}: {e2}",
                    "rc": -1, "cwd": cwd, "truncated": False, "elapsed": 0.0}
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    out, truncated_out = _cap(out)
    err, truncated_err = _cap(err)
    stdout_t = _decode(out)
    stderr_t = _decode(err)
    clean, new_cwd, meta_rc = parse_meta(stdout_t)
    with _lock:
        if new_cwd:
            _session["cwd"] = new_cwd
        cwd_now = _session["cwd"]
    if timed_out:
        stderr_t = (stderr_t + f"\n[REMOTEPS] timeout de {timeout:g}s: proceso terminado"
                    ).strip("\n")
        rc = -1
    else:
        rc = meta_rc if meta_rc is not None else (
            proc.returncode if proc.returncode is not None else -1)
    return {"stdout": clean, "stderr": stderr_t, "rc": rc, "cwd": cwd_now,
            "truncated": truncated_out or truncated_err,
            "elapsed": round(time.time() - t0, 2)}


def expand_path(p: str, base: str = None) -> str:
    if p is None:
        p = ""
    if not isinstance(p, str):
        raise ValueError("path debe ser texto")
    if "\x00" in p:
        raise ValueError("ruta invalida")
    p = os.path.expanduser(p.strip() or ".")
    if not os.path.isabs(p):
        with _lock:
            root = base or _session["cwd"]
        p = os.path.join(root, p)
    return os.path.normpath(p)


def fs_list(p: str = "") -> dict:
    path = expand_path(p)
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            entries.append({"name": e.name, "dir": is_dir,
                            "size": 0 if is_dir else st.st_size,
                            "mtime": int(st.st_mtime)})
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None,
            "entries": entries}


def fs_read(p: str) -> dict:
    path = expand_path(p)
    size = os.path.getsize(path)
    truncated = size > MAX_READ_FILE
    with open(path, "rb") as f:
        data = f.read(MAX_READ_FILE)
    return {"path": path, "content": _decode(data), "size": size,
            "truncated": truncated}


def fs_write(p: str, content: str) -> dict:
    path = expand_path(p)
    data = content.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "path": path, "bytes": len(data)}


def fs_delete(p: str, recursive: bool = False) -> dict:
    path = expand_path(p)
    parent = os.path.dirname(path)
    if parent == path or path in ("", "."):
        raise ValueError("no se puede borrar la raiz")
    if os.path.isdir(path):
        if recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    else:
        os.remove(path)
    return {"ok": True, "path": path}


def fs_mkdir(p: str) -> dict:
    path = expand_path(p)
    os.makedirs(path, exist_ok=True)
    return {"ok": True, "path": path}


def ps_version() -> str:
    global _psver
    if not IS_POWERSHELL:
        return SHELL
    if _psver is None:
        try:
            r = subprocess.run(
                [SHELL, "-NoProfile", "-NonInteractive", "-Command",
                 "$PSVersionTable.PSVersion.ToString()"],
                capture_output=True, timeout=30)
            _psver = _decode(r.stdout).strip() or "?"
        except Exception:
            _psver = "?"
    return _psver


# ---------------------------------------------------------------- tunel integrado


def extract_tunnel_url(line):
    """Busca la URL del quick tunnel dentro una linea de salida de cloudflared.
    Funciona con el formato real ('... INF |  https://xxx.trycloudflare.com').
    Devuelve la URL sin barra final, o None si la linea no tiene ninguna."""
    m = TUNNEL_URL_RE.search(line or "")
    return m.group(0) if m else None


def find_cloudflared(explicit=None):
    """Localiza el binario cloudflared. Con `explicit` valida esa ruta.
    Sin ella: PATH primero y en Windows las carpetas tipicas de instalacion."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    cf = shutil.which("cloudflared")
    if cf:
        return cf
    if os.name == "nt":
        bases = (
            os.environ.get("SystemDrive", "C:") + r"\Program Files\cloudflared",
            os.path.join(os.environ.get("USERPROFILE", ""), ".cloudflared"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        )
        for base in bases:
            for name in ("cloudflared.exe", "cloudflared"):
                cand = os.path.join(base, name) if base else ""
                if cand and os.path.isfile(cand):
                    return cand
    return None


class Tunnel:
    """cloudflared como proceso hijo dentro de servidor.py.

    - Lanza `cloudflared tunnel --url http://127.0.0.1:<port> --no-autoupdate`.
    - Un thread lee stdout+stderr combinado linea a linea; cuando
      extract_tunnel_url encuentra la URL, la imprime y marca url_event.
    - Si cloudflared muere (red, edge, etc.) reinicia con backoff hasta
      MAX_RESTARTS intentos; si sale una URL nueva se imprime igual.
    - stop() manda SIGTERM (kill forzado a los 5s) y corta el ciclo de
      reinicio. main() lo llama al Ctrl+C: una sola terminal apaga
      servidor y tunel juntos.
    """

    MAX_RESTARTS = 5
    BACKOFF = (2, 5, 10, 20, 30)

    def __init__(self, binary, port, quiet=False):
        self.binary = binary
        self.port = port
        self.quiet = quiet
        self.proc = None
        self.url = None
        self.url_event = threading.Event()
        self.stopping = False
        self._thread = None
        self._restarts = 0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _say(self, msg):
        """Diagnostica del tunel: stdout si es modo humano, stderr si --print-url
        (asi un script solo ve la URL cruda en stdout)."""
        print(msg, file=sys.stderr if self.quiet else sys.stdout, flush=True)

    def _spawn(self):
        argv = [self.binary, "tunnel", "--url",
                f"http://127.0.0.1:{self.port}", "--no-autoupdate"]
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                  "text": True, "encoding": "utf-8", "errors": "replace",
                  "bufsize": 1}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(argv, **kwargs)
        except OSError as e:
            self._say(f"[tunel] no se pudo lanzar {self.binary}: {e}")
            self.proc = None
        return self.proc

    def _loop(self):
        while not self.stopping:
            proc = self._spawn()
            if proc is None:
                return
            for raw in iter(proc.stdout.readline, ""):
                url = extract_tunnel_url(raw)
                if url and url != self.url:
                    self.url = url
                    self.url_event.set()
                    if self.quiet:
                        print(url, flush=True)
                    else:
                        print(f"\n[tunel] URL publica: {url}", flush=True)
                        print(f"[tunel] desde la otra PC:  "
                              f"python cliente.py --url {url}", flush=True)
            proc.wait()
            if self.stopping:
                break
            self._restarts += 1
            if self._restarts > self.MAX_RESTARTS:
                self._say("[tunel] cloudflared murio demasiadas veces; "
                          "no se reinicia mas")
                break
            wait_s = self.BACKOFF[min(self._restarts - 1, len(self.BACKOFF) - 1)]
            self._say(f"[tunel] cloudflared cayo; reinicio "
                      f"{self._restarts}/{self.MAX_RESTARTS} en {wait_s}s...")
            time.sleep(wait_s)

    def stop(self):
        self.stopping = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RemotePS/1.0"

    def log_message(self, fmt, *args):
        code = args[1] if len(args) > 1 else "?"
        sys.stderr.write(f"[servidor] {self.command} {self.path} -> {code}\n")

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadReq(400, "el body no es JSON valido")
        if not isinstance(obj, dict):
            raise BadReq(400, "el body debe ser un objeto JSON")
        return obj

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        try:
            if not check_auth(self.headers):
                return self._json({"error": "sin autorizacion"}, 401)
            if route == "/ping":
                with _lock:
                    cwd = _session["cwd"]
                return self._json({
                    "ok": True, "hostname": socket.gethostname(),
                    "user": getpass.getuser(), "platform": platform.platform(),
                    "shell": ps_version(), "cwd": cwd,
                })
            self._json({"error": "ruta desconocida"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        try:
            if not check_auth(self.headers):
                return self._json({"error": "sin autorizacion"}, 401)
            if route == "/fs/upload":
                return self._handle_upload()
            body = self._body()
            if route == "/execute":
                cmd = body.get("cmd")
                if not isinstance(cmd, str) or not cmd.strip():
                    raise BadReq(400, "falta el campo 'cmd'")
                try:
                    to = float(body.get("timeout", DEFAULT_TIMEOUT))
                except (TypeError, ValueError):
                    raise BadReq(400, "timeout invalido")
                to = min(max(to, 1), MAX_TIMEOUT)
                return self._json(run_command(cmd, to))
            return self._dispatch_fs(route, body)
        except BadReq as e:
            self.close_connection = True
            self._json({"error": str(e)}, e.status)
        except FileNotFoundError as e:
            self._json({"error": f"no existe: {getattr(e, 'filename', '') or e}"}, 404)
        except NotADirectoryError as e:
            self._json({"error": f"no es un directorio: {getattr(e, 'filename', '')}"}, 400)
        except IsADirectoryError as e:
            self._json({"error": f"es un directorio: {getattr(e, 'filename', '')}"}, 400)
        except PermissionError:
            self._json({"error": "permiso denegado"}, 403)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except OSError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"error interno: {type(e).__name__}: {e}"}, 500)

    def _dispatch_fs(self, route: str, body: dict):
        if route == "/fs/list":
            return self._json(fs_list(body.get("path", "")))
        if route == "/fs/read":
            p = body.get("path")
            if not isinstance(p, str) or not p:
                raise BadReq(400, "falta 'path'")
            return self._json(fs_read(p))
        if route == "/fs/write":
            p, c = body.get("path"), body.get("content")
            if not isinstance(p, str) or not p:
                raise BadReq(400, "falta 'path'")
            if not isinstance(c, str):
                raise BadReq(400, "falta 'content'")
            return self._json(fs_write(p, c))
        if route == "/fs/delete":
            p = body.get("path")
            if not isinstance(p, str) or not p:
                raise BadReq(400, "falta 'path'")
            return self._json(fs_delete(p, bool(body.get("recursive"))))
        if route == "/fs/mkdir":
            p = body.get("path")
            if not isinstance(p, str) or not p:
                raise BadReq(400, "falta 'path'")
            return self._json(fs_mkdir(p))
        if route == "/fs/download":
            p = body.get("path")
            if not isinstance(p, str) or not p:
                raise BadReq(400, "falta 'path'")
            return self._send_file(p)
        self._json({"error": "ruta desconocida"}, 404)

    def _send_file(self, p: str):
        path = expand_path(p)
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise BadReq(413, "archivo demasiado grande")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle_upload(self):
        rp = unquote(self.headers.get("X-Remote-Path", ""))
        if not rp:
            raise BadReq(400, "falta el header X-Remote-Path")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise BadReq(400, "body vacio")
        if n > MAX_UPLOAD:
            self.close_connection = True
            return self._json({"error": "archivo demasiado grande"}, 413)
        path = expand_path(rp)
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".remoteps-", dir=parent)
        try:
            remaining = n
            with os.fdopen(fd, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise BadReq(400, "body truncado")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._json({"ok": True, "path": path, "bytes": n})


def serve(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main():
    ap = argparse.ArgumentParser(
        prog="servidor.py",
        description="RemotePS: servidor local (HTTP en loopback + ejecutor de "
                    "shell). Sin flags se comporta como siempre: solo escucha "
                    "en 127.0.0.1 y cloudflared corre aparte.")
    ap.add_argument("--tunnel", action="store_true",
                    help="lanza cloudflared como hijo, captura su URL publica "
                         "y la imprime aqui (una sola terminal)")
    ap.add_argument("--cloudflared", metavar="RUTA", default=None,
                    help="ruta explicita al binario cloudflared "
                         "(por defecto se busca en PATH y carpetas tipicas)")
    ap.add_argument("--print-url", action="store_true",
                    help="imprime solo la URL cuando aparece (util para scripts)")
    args = ap.parse_args()

    use_tunnel = args.tunnel or args.print_url
    cf_bin = None
    if use_tunnel:
        cf_bin = find_cloudflared(args.cloudflared)
        if not cf_bin:
            print("[servidor] ERROR: no encuentro cloudflared"
                  + (f" en {args.cloudflared}" if args.cloudflared
                     else " en PATH ni carpetas tipicas"),
                  file=sys.stderr)
            print("[servidor] bajalo de: https://developers.cloudflare.com/"
                  "cloudflare-one/connections/connect-networks/downloads/"
                  "  o pasala ruta con --cloudflared <ruta>", file=sys.stderr)
            sys.exit(1)

    quiet = args.print_url  # stdout reservado solo para la URL
    out = sys.stderr if quiet else sys.stdout

    def say(msg):
        print(msg, file=out, flush=True)

    say("=" * 64)
    say("  RemotePS — servidor")
    say(f"  Shell      : {SHELL}" + (" (powershell)" if IS_POWERSHELL else ""))
    say(f"  Escucha    : http://{HOST}:{PORT}  (solo loopback)")
    say("  Autenticac : " + (
        "TOKEN activo" if AUTH_TOKEN else "SIN token (la URL del tunel es la credencial)"))
    say(f"  Inicio     : {sys.argv[0]}")
    if cf_bin:
        say(f"  Tunel      : automatico con {cf_bin} (hijo de este proceso)")
    else:
        say("  Requiere cloudflared: cloudflared tunnel --url http://localhost:%d" % PORT)
    say("  Ctrl+C para detener.")
    if os.name == "nt":
        say("[nota] ejecutalo como Administrador si necesitas comandos elevados")
    say("=" * 64)

    try:
        httpd = serve()
    except OSError as e:
        print(f"[servidor] ERROR: no se pudo escuchar en {HOST}:{PORT}: {e}",
              file=sys.stderr)
        print("[servidor] ya hay otra instancia corriendo: cerrala"
              " (Ctrl+C alli o: pkill -f servidor.py)", file=sys.stderr)
        print(f"[servidor] ...o usá otro puerto: REMOTEPS_PORT={PORT + 1} "
              f"python {sys.argv[0]}", file=sys.stderr)
        sys.exit(1)

    tunnel = None
    if cf_bin:
        tunnel = Tunnel(cf_bin, PORT, quiet=quiet)
        tunnel.start()
        say("[tunel] iniciando cloudflared... la URL puede tardar 10-30s "
            "y cambia en cada arranque")

    def _termina(signum, frame):
        raise KeyboardInterrupt

    for _sig in (signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _termina)
            except (ValueError, OSError):
                pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        say("\n[servidor] apagando...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        if tunnel:
            say("[tunel] deteniendo cloudflared...")
            tunnel.stop()
            say("[tunel] listo: la URL publica ya no responde")


if __name__ == "__main__":
    main()
