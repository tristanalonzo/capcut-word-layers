"""
CapCut Word Layers — double-click to open the window.

Starts a small local server (this computer only) and opens the editor in an
app window. Nothing is sent anywhere. Problems are written to wordlayers.log
next to this file.

Options (only needed for troubleshooting):
    python wordlayers.pyw --no-window      print the address instead of opening a window
    python wordlayers.pyw --project PATH   open this project folder straight away
"""
import argparse
import json
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
from engine import EditError, Editor  # noqa: E402

LOG = HERE / "wordlayers.log"
TOKEN = secrets.token_urlsafe(24)
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2"}
STATE = {"editor": None, "last_seen": None, "closing": None, "port": 0, "fonts": None}
LOCK = threading.Lock()


def log(text):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


# ------------------------------------------------------------------ fonts

def local_font(path):
    """A font file on a local drive. Network paths are refused."""
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return None
    p = Path(text)
    return p if p.suffix.lower() in FONT_EXT and p.is_file() else None


def font_name(path):
    """Read a font's display name from the file, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            count = struct.unpack(">H", head[4:6])[0]
            for _ in range(min(count, 64)):
                tag, _, offset, _ = struct.unpack(">4sIII", f.read(16))
                if tag == b"name":
                    break
            else:
                return None
            f.seek(offset)
            _, n, strings = struct.unpack(">HHH", f.read(6))
            records = [struct.unpack(">HHHHHH", f.read(12)) for _ in range(min(n, 200))]
            best = None
            for plat, _, lang, name_id, length, off in records:
                if name_id != 4:
                    continue
                rank = 0 if (plat == 3 and lang == 0x409) else 1 if plat == 3 else 2
                if best is None or rank < best[0]:
                    best = (rank, plat, length, off)
            if not best:
                return None
            f.seek(offset + strings + best[3])
            raw = f.read(best[2])
            name = raw.decode("utf-16-be" if best[1] in (0, 3) else "latin-1", "ignore").strip()
            return name or None
    except (OSError, struct.error, ValueError):
        return None


def list_fonts(extra_paths=()):
    """Fonts the user can pick: Windows fonts, CapCut's fonts, fonts in the project."""
    if STATE["fonts"] is None:
        folders = []
        if os.environ.get("WINDIR"):
            folders.append(Path(os.environ["WINDIR"]) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            folders.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
            folders.append(Path(local) / "CapCut" / "User Data" / "Cache" / "effect")
            folders.append(Path(local) / "CapCut" / "Apps")
        found = {}
        for folder in folders:
            if not folder.is_dir():
                continue
            deep = "CapCut" in folder.parts
            try:
                for i, f in enumerate(folder.rglob("*") if deep else folder.iterdir()):
                    if i > 60000 or len(found) > 1500:
                        break
                    if f.suffix.lower() in (".ttf", ".otf") and f.is_file():
                        found[str(f).replace("\\", "/")] = None
            except OSError:
                pass
        STATE["fonts"] = found
    fonts = STATE["fonts"]
    for p in extra_paths:
        if p and p not in fonts and local_font(p):
            fonts[p] = None
    for p in fonts:
        if fonts[p] is None:
            fonts[p] = font_name(p) or Path(p).stem
    return sorted(({"path": p, "name": n} for p, n in fonts.items()), key=lambda d: d["name"].lower())


# ------------------------------------------------------------------ api

def handle(op, a):
    """Run one request from the window. Returns a dict that is sent back as JSON."""
    if op == "ping":
        return {}
    if op == "boot":
        ed = STATE["editor"]
        return {"projects": engine.list_projects(), "view": ed.view() if ed else None}
    if op == "open":
        path = str(a.get("path", "")).strip().strip('"')
        if not path:
            raise EditError("Choose a project folder.")
        STATE["editor"] = Editor(path, int(a.get("doc", 0)))
        return {"view": STATE["editor"].view()}
    ed = STATE["editor"]
    if ed is None:
        raise EditError("Open a project first.")
    ids, co = a.get("ids"), a.get("coalesce")
    result = None
    if op == "fonts":
        used = {c["style"]["font"] for t in ed.view()["tracks"] for c in t["clips"]}
        return {"fonts": list_fonts(sorted(used))}
    if op == "split":
        result = ed.split(ids)
    elif op == "layer":
        result = ed.layer(ids)
    elif op == "unlayer":
        result = ed.unlayer(ids)
    elif op == "set_time":
        ed.set_time(a["id"], a.get("start"), a.get("dur"), a.get("track"))
    elif op == "shift_time":
        ed.shift_time(ids, a["delta"], coalesce=co)
    elif op == "nudge_pos":
        ed.nudge_pos(ids, a.get("dx", 0), a.get("dy", 0), coalesce=co)
    elif op == "set_text":
        ed.set_text(a["id"], a["text"])
    elif op == "delete":
        ed.delete(ids)
    elif op == "lock":
        result = ed.lock(ids, a.get("value"))
    elif op == "style":
        result = ed.style(ids, a["patch"], coalesce=co)
    elif op == "copy_style":
        ed.copy_style(a["id"])
    elif op == "paste_style":
        result = ed.paste_style(ids)
    elif op == "settings":
        ed.set_settings(a.get("patch"))
    elif op == "undo":
        ed.undo()
    elif op == "redo":
        ed.redo()
    elif op == "save":
        result = ed.save(force=bool(a.get("force")), open_ok=bool(a.get("open_ok")))
    elif op == "restore":
        name = engine.restore_latest_backup(ed.project)
        STATE["editor"] = ed = Editor(ed.project, ed.doc_index)
        result = {"backup": name}
    elif op == "reload":
        STATE["editor"] = ed = Editor(ed.project, ed.doc_index)
    else:
        raise EditError(f"Unknown action: {op}")
    return {"result": result, "view": ed.view()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        # only this window may talk to the server
        return self.headers.get("Host", "") in (f"127.0.0.1:{STATE['port']}", f"localhost:{STATE['port']}")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self._local() or q.get("t", [""])[0] != TOKEN:
            return self._send(403, b"forbidden", "text/plain")
        if url.path == "/":
            page = (HERE / "ui.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if url.path == "/font":
            p = local_font(q.get("p", [""])[0])
            if p:
                return self._send(200, p.read_bytes(), "font/ttf", cache=True)
            return self._send(404, b"not found", "text/plain")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/bye":
            if self._local() and parse_qs(url.query).get("t", [""])[0] == TOKEN:
                STATE["closing"] = time.time() + 4
            return self._send(200, b"{}")
        if not self._local() or self.headers.get("X-Token") != TOKEN or url.path != "/api":
            return self._send(403, b"forbidden", "text/plain")
        STATE["last_seen"], STATE["closing"] = time.time(), None
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with LOCK:
                out = handle(body.get("op"), body.get("args") or {})
            out["ok"] = True
        except EditError as e:
            out = {"ok": False, "error": str(e)}
        except Exception as e:
            log(traceback.format_exc())
            out = {"ok": False, "error": f"Something went wrong ({type(e).__name__}: {e}). "
                                         f"Details are in wordlayers.log."}
        if not out["ok"] and STATE["editor"] is not None:
            out["view"] = STATE["editor"].view()
        self._send(200, json.dumps(out).encode("utf-8"))


# ------------------------------------------------------------------ window

def open_window(url):
    """Open the editor as its own window (Edge or Chrome app mode), else in the browser."""
    candidates = []
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                           Path(base) / "Google/Chrome/Application/chrome.exe"]
    for exe in candidates:
        if exe.is_file():
            try:
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1500,940"])
                return
            except OSError:
                pass
    webbrowser.open(url)


def watchdog(server):
    """Stop the server once the window is gone."""
    started = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        closing, seen = STATE["closing"], STATE["last_seen"]
        if ((closing and now > closing) or (seen and now - seen > 150)
                or (seen is None and now - started > 300)):
            server.shutdown()
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--project")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    if args.project:
        STATE["editor"] = Editor(args.project)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    STATE["port"] = server.server_address[1]
    url = f"http://127.0.0.1:{STATE['port']}/?t={TOKEN}"
    if args.no_window:
        print(url, flush=True)
    else:
        open_window(url)
        threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log(traceback.format_exc())
        try:  # there is no terminal when double-clicked, so show a dialog
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("CapCut Word Layers", f"Could not start:\n{exc}\n\nDetails: {LOG}")
        except Exception:
            pass
