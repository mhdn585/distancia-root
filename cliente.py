#!/usr/bin/env python3
"""cliente.py — RemotePS | PC de origen (multiplataforma).

REPL interactivo sobre prompt_toolkit que envia comandos PowerShell al servidor
a traves de la URL publica del tunel de Cloudflare (cloudflared quick tunnel).

Uso:
  python cliente.py --url https://xxxx.trycloudflare.com
  REMOTEPS_URL=https://xxxx.trycloudflare.com python cliente.py

Dependencias: solo prompt_toolkit (ver requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from prompt_toolkit import Application, PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.application import get_app
except ImportError:
    sys.exit("falta prompt_toolkit. Instalalo con: pip install -r requirements.txt")

VERSION = "1.0"
HISTORY_FILE = os.path.expanduser("~/.remoteps-history")
MAX_UPLOAD = 200 * 1024 * 1024

LOCAL_COMMANDS = ["salir", "ping", "cd", "ls", "cat", "edit", "get", "put",
                  "rm", "mkdir", "url", "timeout", "clear", "help"]


# ------------------------------------------------------------------ helpers puros
def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def parse_input(line: str):
    s = line.strip()
    if not s:
        return None
    if s in ("salir", "exit", "quit"):
        return ("local", "salir", "")
    if s.startswith("/"):
        parts = s[1:].split(None, 1)
        name = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        return ("local", name, rest)
    return ("remote", line.rstrip("\n"), None)


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "?"


def shorten(s: str, n: int) -> str:
    if not s:
        return "?"
    return s if len(s) <= n else "..." + s[-(n - 3):]


def remote_pjoin(parent: str, name: str) -> str:
    sep = "\\" if "\\" in parent else "/"
    return parent.rstrip("\\/") + sep + name


def local_basename(p: str) -> str:
    return os.path.basename(p.replace("\\", "/").rstrip("/\\")) or "archivo"


def format_result(r: dict):
    parts = []
    out = (r.get("stdout") or "").rstrip("\n")
    err = (r.get("stderr") or "").rstrip("\n")
    rc = r.get("rc")
    if out:
        parts.append((out, ""))
    if err:
        parts.append((err, "31;1"))
    if r.get("truncated"):
        parts.append(("! la salida fue truncada en el servidor", "33"))
    if rc not in (0, None):
        parts.append((f"[rc={rc}]", "31;1"))
    return parts


# ------------------------------------------------------------------ transporte
class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, base: str, cmd_timeout: int = 90, token: str = ""):
        self.base = base
        self.cmd_timeout = cmd_timeout
        self.token = token
        self.ok = False
        self.host = ""
        self.user = ""
        self.cwd = ""
        self.shell = ""
        self.last = (None, None)

    def _req(self, method, path, payload=None, raw_body=None, extra=None,
             timeout=60):
        url = join_url(self.base, path)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/octet-stream"
        if extra:
            headers.update(extra)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                self.ok = True
                if ctype.startswith("application/json"):
                    return json.loads(body.decode("utf-8"))
                return body
        except urllib.error.HTTPError as e:
            self.ok = False
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                msg = None
            raise RemoteError(msg or f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            self.ok = False
            raise RemoteError(
                f"sin conexion ({e.reason}). Revisa: URL del tunel, "
                "cloudflared vivo, servidor corriendo")
        except TimeoutError:
            self.ok = False
            raise RemoteError(
                "timeout de red: el comando remoto puede seguir corriendo")
        except OSError as e:
            self.ok = False
            raise RemoteError(f"error de conexion: {e}")

    def ping(self):
        r = self._req("GET", "/ping", timeout=20)
        self.host = r.get("hostname", "")
        self.user = r.get("user", "")
        self.cwd = r.get("cwd", "")
        self.shell = r.get("shell", "")
        return r

    def execute(self, cmd):
        r = self._req("POST", "/execute",
                      {"cmd": cmd, "timeout": self.cmd_timeout},
                      timeout=self.cmd_timeout + 20)
        self.cwd = r.get("cwd", self.cwd)
        self.last = (r.get("rc"), r.get("elapsed"))
        return r

    def fs(self, route, payload=None, **extra):
        return self._req("POST", route, payload, timeout=120, **extra)

    def fs_list(self, path=""):
        return self.fs("/fs/list", {"path": path})

    def fs_read(self, path):
        return self.fs("/fs/read", {"path": path})

    def fs_write(self, path, content):
        return self.fs("/fs/write", {"path": path, "content": content})

    def fs_delete(self, path, recursive=False):
        return self.fs("/fs/delete", {"path": path, "recursive": recursive})

    def fs_mkdir(self, path):
        return self.fs("/fs/mkdir", {"path": path})

    def download(self, remote_path, local_path):
        data = self._req("POST", "/fs/download", {"path": remote_path},
                         timeout=600)
        if not isinstance(data, (bytes, bytearray)):
            raise RemoteError("respuesta inesperada del servidor")
        with open(local_path, "wb") as f:
            f.write(data)
        return len(data)

    def upload(self, local_path, remote_path):
        size = os.path.getsize(local_path)
        if size > MAX_UPLOAD:
            raise RemoteError(f"archivo demasiado grande ({human_size(size)})")
        with open(local_path, "rb") as f:
            data = f.read()
        return self._req(
            "POST", "/fs/upload", raw_body=data,
            extra={"X-Remote-Path": urllib.parse.quote(remote_path)},
            timeout=600)


# ------------------------------------------------------------------ render REPL
def ansi(text, code):
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def print_result(r):
    for text, code in format_result(r):
        print(ansi(text, code))


def print_line(text, code=""):
    print(ansi(text, code))


def enable_vt():
    if os.name != "nt":
        return
    try:
        import ctypes
        h = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


HELP_TEXT = """\
Comandos locales (prefijo /). Cualquier otra linea se ejecuta en PowerShell remoto.

  /salir              salir del cliente (o escribir: salir)
  /ping               probar conexion e identificar la PC remota
  /cd <ruta>          cambiar de directorio remoto
  /ls [ruta]          abrir el EXPLORADOR de archivos interactivo
  /cat <ruta>         mostrar un archivo de texto remoto
  /edit <ruta>        descargar->editar con $EDITOR->subir de vuelta
  /get <ruta> [local] descargar un archivo a la PC local
  /put <local> <ruta> subir un archivo a la PC remota
  /rm [-r] <ruta>     borrar archivo o carpeta remota (pide confirmacion)
  /mkdir <ruta>       crear carpeta remota
  /url <nueva>        cambiar la URL del tunel en caliente
  /timeout <1-300>    segundos de espera por comando (default 90)
  /clear              limpiar pantalla
  /help               esta ayuda

En el explorador: ↑/↓ mover · Enter entrar/preview · ←/Backspace subir · d bajar
  · u subir · m nueva carpeta · x borrar (x de nuevo confirma) · e editar · q salir"""


# ------------------------------------------------------------------ explorador
def browse(rm: Remote, start_path: str = ""):
    """Explorador a pantalla completa. Retorna None, ('edit', path) o ('get', path)."""
    st = {"path": "", "data": None, "idx": 0, "preview": None,
          "mode": None, "input": "", "msg": ""}

    def load(path):
        try:
            data = rm.fs_list(path)
            st["data"] = data
            st["path"] = data["path"]
            st["idx"] = 0
            st["preview"] = None
            st["msg"] = ""
        except RemoteError as e:
            st["msg"] = str(e)

    def entries():
        return (st["data"] or {}).get("entries", [])

    def selected():
        es = entries()
        return es[st["idx"]] if es else None

    def sel_path():
        e = selected()
        return remote_pjoin(st["path"], e["name"]) if e else None

    def set_mode(mode, placeholder=""):
        st["mode"] = mode
        st["input"] = ""
        st["placeholder"] = placeholder

    def commit_input():
        text = st["input"].strip()
        mode = st["mode"]
        st["mode"] = None
        st["input"] = ""
        if not text:
            return
        try:
            if mode == "mkdir":
                rm.fs_mkdir(remote_pjoin(st["path"], text))
                st["msg"] = f"creada: {text}"
            elif mode == "upload":
                lp = os.path.expanduser(text)
                if not os.path.isfile(lp):
                    raise RemoteError(f"no existe local: {lp}")
                rm.upload(lp, remote_pjoin(st["path"], local_basename(lp)))
                st["msg"] = f"subido: {local_basename(lp)}"
            load(st["path"])
        except RemoteError as e:
            st["msg"] = str(e)

    def delete_selected(app):
        e = selected()
        if not e:
            return
        p = sel_path()
        try:
            rm.fs_delete(p, recursive=True)
            st["msg"] = f"borrado: {e['name']}"
        except RemoteError as ex:
            st["msg"] = str(ex)
        load(st["path"])

    def open_preview():
        e = selected()
        if not e or e["dir"]:
            return
        try:
            data = rm.fs_read(sel_path())
            text = data.get("content", "")
            lines = text.splitlines()
            st["preview"] = {"path": sel_path(), "lines": lines,
                             "off": 0, "size": data.get("size", len(text)),
                             "truncated": data.get("truncated")}
        except RemoteError as ex:
            st["msg"] = str(ex)

    kb = KeyBindings()

    @kb.add("up")
    def _up(_):
        if st["preview"]:
            st["preview"]["off"] = max(0, st["preview"]["off"] - 1)
        else:
            st["idx"] = max(0, st["idx"] - 1)
            st["msg"] = ""

    @kb.add("down")
    def _down(_):
        if st["preview"]:
            pv = st["preview"]
            pv["off"] = min(max(0, len(pv["lines"]) - 5), pv["off"] + 1)
        else:
            st["idx"] = min(max(0, len(entries()) - 1), st["idx"] + 1)
            st["msg"] = ""

    @kb.add("left")
    @kb.add("backspace")
    def _parentdir(_):
        if st["mode"]:
            st["mode"] = None
            st["input"] = ""
        elif st["preview"]:
            st["preview"] = None
        else:
            parent = (st["data"] or {}).get("parent")
            if parent:
                load(parent)

    @kb.add("right")
    @kb.add("enter")
    def _act(_):
        if st["mode"]:
            commit_input()
            return
        if st["preview"]:
            st["preview"] = None
            return
        e = selected()
        if not e:
            return
        if e["dir"]:
            load(remote_pjoin(st["path"], e["name"]))
        else:
            open_preview()

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-g")
    def _quit(event):
        if st["mode"]:
            st["mode"] = None
            st["input"] = ""
        elif st["preview"]:
            st["preview"] = None
        else:
            event.app.exit()

    @kb.add("c-c")
    def _cc(event):
        event.app.exit()

    @kb.add("d")
    def _dl(event):
        if st["mode"]:
            return
        e = selected()
        if e and not e["dir"]:
            event.app.exit(("get", sel_path()))

    @kb.add("e")
    def _ed(event):
        if st["mode"]:
            return
        e = selected()
        if e and not e["dir"]:
            event.app.exit(("edit", sel_path()))

    @kb.add("x")
    def _del(event):
        if st["mode"] == "confirm":
            delete_selected(event.app)
            st["mode"] = None
        elif selected():
            st["mode"] = "confirm"
            st["msg"] = f"¿borrar {selected()['name']}? otra 'x' confirma · otra tecla cancela"
        else:
            st["mode"] = None

    @kb.add("m")
    def _mk(event):
        if not st["mode"]:
            set_mode("mkdir", "nueva carpeta: ")

    @kb.add("u")
    def _upload(event):
        if not st["mode"]:
            set_mode("upload", "ruta local a subir: ")

    @kb.add("r")
    def _rl(event):
        if not st["mode"]:
            load(st["path"])

    @kb.add("<any>")
    def _key(event):
        ch = event.data or ""
        if st["mode"] == "confirm":
            st["mode"] = None
            st["msg"] = ""
            return
        if st["mode"] and ch.isprintable():
            st["input"] += ch

    def header():
        n = len(entries())
        base = f" EXPLORADOR · {st['path']} · {n} items "
        if st["preview"]:
            pv = st["preview"]
            base = (f" PREVIEW · {pv['path']} · {human_size(pv['size'])}"
                    + (" (truncado)" if pv["truncated"] else ""))
        return [("class:reverse", base[:200])]

    def body():
        if st["preview"]:
            pv = st["preview"]
            try:
                rows = get_app().output.get_size().rows
            except Exception:
                rows = 40
            avail = max(rows - 3, 5)
            out = []
            for i, ln in enumerate(pv["lines"][pv["off"]:pv["off"] + avail]):
                out.append(("ansibrightblack", f"{pv['off']+i+1:>5} │ "))
                out.append(("", ln[:250] + "\n"))
            if not out:
                out = [("", "(vacio)")]
            return out
        es = entries()
        if not es:
            return [("", "(vacio)")] if st["data"] else \
                [("ansibrightred", st["msg"] or "cargando...")]
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 40
        avail = max(rows - 4, 5)
        idx = st["idx"]
        first = max(0, min(idx - avail // 2, len(es) - avail))
        out = []
        for i in range(first, min(len(es), first + avail)):
            e = es[i]
            mark = ">" if i == idx else " "
            name = e["name"] + ("/" if e["dir"] else "")
            style = "class:selected" if i == idx else ""
            extra = "        " if e["dir"] else f"{human_size(e['size']):>9}"
            mtime = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(e.get("mtime") or 0))
            out.append((style, f"{mark} {name:<48.48} {extra}  {mtime}\n"))
        return out

    def footer():
        if st["mode"] == "confirm":
            txt = st["msg"]
            return [("ansibrightred bold", f" {txt[:150]}")]
        if st["mode"]:
            return [("ansibrightyellow", f" {st['placeholder']}{st['input']}▏ "
                     " Enter confirma · Esc cancela")]
        left = ("↑↓ mover · Entrar abrir/preview · ← subir · d bajar · u subir "
                "· m mkdir · x borrar · e editar · r recargar · q salir")
        toks = [("ansibrightblack", f" {left[:168]}")]
        if st["msg"]:
            toks.append(("ansibrightyellow", f"  |  {shorten(st['msg'], 120)}"))
        return toks

    layout = Layout(HSplit([
        Window(FormattedTextControl(header), height=1, style="class:reverse"),
        Window(FormattedTextControl(body), wrap_lines=False),
        Window(FormattedTextControl(footer), height=1),
    ]))
    app = Application(layout=layout, key_bindings=kb, full_screen=True)
    load(start_path or ".")
    return app.run()


# ------------------------------------------------------------------ acciones REPL
def run_edit(rm: Remote, path: str):
    try:
        data = rm.fs_read(path)
    except RemoteError as e:
        print_line(str(e), "31;1")
        return
    tmpdir = tempfile.mkdtemp(prefix="remoteps-")
    local = os.path.join(tmpdir, local_basename(path))
    with open(local, "w", encoding="utf-8", newline="") as f:
        f.write(data.get("content", ""))
    ed = os.environ.get("EDITOR")
    if ed:
        cmd = shlex.split(ed) + [local]
    elif os.name == "nt":
        cmd = ["cmd", "/c", "start", "/wait", "", "notepad", local]
    else:
        cmd = ["vi", local]
    print_line(f"abriendo {' '.join(cmd)} ...", "2")
    try:
        subprocess.call(cmd)
    except OSError as e:
        print_line(f"no se pudo abrir el editor: {e}", "31;1")
    with open(local, "r", encoding="utf-8", newline="") as f:
        nuevo = f.read()
    if nuevo == data.get("content"):
        print_line("sin cambios", "2")
    else:
        try:
            r = rm.fs_write(path, nuevo)
            print_line(f"guardado en remoto: {r['path']} ({r['bytes']} bytes)", "32")
        except RemoteError as e:
            print_line(f"fallo al guardar: {e} (copia local: {local})", "31;1")
            return
    shutil.rmtree(tmpdir, ignore_errors=True)


def ask(prompt_text, default=""):
    try:
        v = input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def handle_local(rm: Remote, name: str, rest: str) -> bool:
    """Devuelve False para salir."""
    try:
        if name in ("salir", "exit", "quit", ""):
            return False
        if name == "help":
            print(HELP_TEXT)
        elif name == "clear":
            print("\x1b[2J\x1b[H", end="")
        elif name == "ping":
            r = rm.ping()
            print_line(
                f"ONLINE · {r['hostname']} · user {r['user']} · shell {r['shell']}"
                f" · cwd {r['cwd']}", "32")
        elif name == "cd":
            if not rest:
                print_line(rm.cwd or "?", "36")
            else:
                r = rm.execute(f"cd {rest}")
                if r.get("rc") == 0:
                    print_line(f"cwd: {rm.cwd}", "2")
                else:
                    print_result(r)
        elif name == "ls":
            target = rest or ""
            result = browse(rm, target)
            if isinstance(result, tuple):
                kind, path = result
                if kind == "get":
                    do_get(rm, path, "")
                elif kind == "edit":
                    run_edit(rm, path)
        elif name == "cat":
            if not rest:
                print_line("uso: /cat <ruta>", "33")
            else:
                data = rm.fs_read(rest)
                print(data.get("content", "").rstrip("\n"))
                if data.get("truncated"):
                    print_line("(archivo truncado por tamano)", "33")
        elif name == "edit":
            if not rest:
                print_line("uso: /edit <ruta>", "33")
            else:
                run_edit(rm, rest)
        elif name == "get":
            parts = rest.split()
            if not parts:
                print_line("uso: /get <ruta remota> [local]", "33")
            else:
                do_get(rm, parts[0], parts[1] if len(parts) > 1 else "")
        elif name == "put":
            parts = rest.split(None, 1)
            if len(parts) != 2:
                print_line("uso: /put <local> <ruta remota>", "33")
            else:
                lp = os.path.expanduser(parts[0])
                if not os.path.isfile(lp):
                    print_line(f"no existe: {lp}", "31;1")
                else:
                    r = rm.upload(lp, parts[1])
                    print_line(f"subido {human_size(r['bytes'])} -> {r['path']}", "32")
        elif name == "rm":
            recursive = False
            target = rest
            if target.startswith("-r "):
                recursive, target = True, target[3:]
            if not target:
                print_line("uso: /rm [-r] <ruta>", "33")
            elif ask(f"¿borrar {target}? s/N: ").lower() in ("s", "si", "sí", "y"):
                rm.fs_delete(target, recursive)
                print_line(f"borrado: {target}", "32")
        elif name == "mkdir":
            if rest:
                r = rm.fs_mkdir(rest)
                print_line(f"creada: {r['path']}", "32")
            else:
                print_line("uso: /mkdir <ruta>", "33")
        elif name == "url":
            if rest:
                rm.base = rest if rest.startswith("http") else "https://" + rest
                r = rm.ping()
                print_line(f"nueva URL OK · {r['hostname']}", "32")
            else:
                print_line(f"URL actual: {rm.base}", "36")
        elif name == "timeout":
            try:
                v = max(1, min(300, int(rest)))
                rm.cmd_timeout = v
                print_line(f"timeout por comando: {v}s", "32")
            except ValueError:
                print_line("uso: /timeout <1-300>", "33")
        else:
            print_line(f"comando desconocido: /{name} · /help para la lista", "33")
    except RemoteError as e:
        print_line(f"error: {e}", "31;1")
    except OSError as e:
        print_line(f"error local: {e}", "31;1")
    return True


def do_get(rm: Remote, remote_path: str, local_path: str):
    dest = local_path or local_basename(remote_path)
    n = rm.download(remote_path, dest)
    print_line(f"descargado {human_size(n)} -> {os.path.abspath(dest)}", "32")


# ------------------------------------------------------------------ main
BANNER = r"""\
┌──────────────────────────────────────────────────────────────┐
│  RemotePS {v:<4} · terminal PowerShell remota via Cloudflare   │
│  /help para los comandos · 'salir' para terminar             │
└──────────────────────────────────────────────────────────────┘"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="RemotePS cliente")
    ap.add_argument("--url", default=os.environ.get("REMOTEPS_URL", ""),
                    help="URL publica del tunel (https://xxx.trycloudflare.com)")
    ap.add_argument("--timeout", type=int, default=90,
                    help="segundos maximos por comando remoto")
    ap.add_argument("--token", default=os.environ.get("REMOTEPS_TOKEN", ""),
                    help="token Bearer (o env REMOTEPS_TOKEN)")
    args = ap.parse_args(argv)

    base = args.url or ask("URL del tunel: ")
    if not base:
        sys.exit("sin URL: pasala con --url, REMOTEPS_URL o en el prompt")
    if not base.startswith("http"):
        base = "https://" + base

    rm = Remote(base, cmd_timeout=max(1, min(300, args.timeout)),
                token=args.token)
    enable_vt()
    print(BANNER.format(v=VERSION))
    if rm.token:
        print_line("enviando token Bearer en cada request", "2")
    try:
        r = rm.ping()
        print_line(f"conectado: {r['hostname']} (user {r['user']}, "
                   f"shell {r['shell']})", "32")
    except RemoteError as e:
        print_line(str(e), "31;1")
        print_line("podes seguir intentando con /ping", "33")

    try:
        history = FileHistory(HISTORY_FILE)
    except Exception:
        history = InMemoryHistory()
    def get_prompt():
        return [
            ("ansibrightgreen bold", f"{rm.host or '?'} "),
            ("ansibrightcyan", shorten(rm.cwd or "?", 60)),
            ("bold", " > "),
        ]

    session = PromptSession(
        history=history,
        completer=WordCompleter(["/" + c for c in LOCAL_COMMANDS],
                                ignore_case=True),
    )

    while True:
        try:
            line = session.prompt(get_prompt(), bottom_toolbar=toolbar(rm))
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        parsed = parse_input(line)
        if not parsed:
            continue
        kind, name, rest = parsed
        if kind == "local":
            if not handle_local(rm, name, rest):
                break
            continue
        try:
            r = rm.execute(name)
        except KeyboardInterrupt:
            print_line("cancelado por el usuario "
                       "(el comando remoto puede seguir corriendo)", "33")
            continue
        except RemoteError as e:
            print_line(f"error: {e}", "31;1")
            continue
        print_result(r)
    print_line("adios", "2")


def toolbar(rm: Remote):
    conn = [("ansibrightgreen bold" if rm.ok else "ansibrightred bold",
             f" {rm.host or 'SIN CONEXION'} ")]
    rc, dt = rm.last
    rc_tok = ("ansibrightgreen", " rc:0 ") if rc in (0, None) else \
        ("ansibrightred", f" rc:{rc} {dt}s ")
    return (conn
            + [("", f" {rm.base[:56]} "),
               ("ansibrightmagenta", f" cwd: {shorten(rm.cwd, 40)} "),
               rc_tok,
               ("ansibrightblack", " /help  /ls explorador ")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nadios")
