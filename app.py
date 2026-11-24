"""
Beamly - beam files from your PC to any phone, tablet or laptop on the same Wi-Fi.

Desktop window  : pywebview  -> http://127.0.0.1:<port>/desk   (local only)
Phones / others : browser    -> http://<lan-ip>/<pin>  (scan the QR; port 80 when free)
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
import uuid
import zipfile
from collections import OrderedDict, deque
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template, request, send_file)

try:
    import qrcode
    import qrcode.image.svg
except ImportError:  # pragma: no cover
    qrcode = None

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

APP_NAME = "Beamly"
VERSION = "1.0.0"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CONF_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONF_FILE = CONF_DIR / "config.json"
DEFAULT_PORT = 8765
COOKIE = "beamly_key"

# --------------------------------------------------------------------------- #
#  Config                                                                      #
# --------------------------------------------------------------------------- #
_lock = threading.RLock()


def _new_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _default_config() -> dict:
    return {
        "port": DEFAULT_PORT,
        "pin": _new_pin(),
        "require_pin": True,
        "allow_upload": True,
        "receive_dir": str(Path.home() / "Downloads" / APP_NAME),
        "preferred_ip": "",
        "shares": [],
    }


def load_config() -> dict:
    cfg = _default_config()
    try:
        if CONF_FILE.exists():
            cfg.update(json.loads(CONF_FILE.read_text("utf-8")))
    except Exception:
        pass
    # drop shares whose path vanished
    cfg["shares"] = [s for s in cfg["shares"] if Path(s["path"]).exists()]
    return cfg


def save_config() -> None:
    with _lock:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        CONF_FILE.write_text(json.dumps(CONFIG, indent=2), "utf-8")


CONFIG = load_config()

# --------------------------------------------------------------------------- #
#  Runtime state (activity, devices, clipboard)                                #
# --------------------------------------------------------------------------- #
ACTIVITY: deque = deque(maxlen=200)
DEVICES: dict = {}
MESSAGES: deque = deque(maxlen=100)
STATS = {"downloads": 0, "uploads": 0, "bytes_out": 0, "bytes_in": 0}
FAILED: dict = {}  # ip -> [count, first_ts]
THUMBS: "OrderedDict[str, bytes]" = OrderedDict()
SERVER = {"port": DEFAULT_PORT, "started": time.time()}


def device_label(ua: str) -> tuple[str, str]:
    u = (ua or "").lower()
    if "iphone" in u:
        return "iPhone", "phone"
    if "ipad" in u:
        return "iPad", "tablet"
    if "android" in u:
        return ("Android tablet", "tablet") if "mobile" not in u else ("Android phone", "phone")
    if "windows" in u:
        return "Windows PC", "laptop"
    if "mac os" in u or "macintosh" in u:
        return "Mac", "laptop"
    if "linux" in u:
        return "Linux PC", "laptop"
    return "Device", "laptop"


def touch_device() -> None:
    ip = request.remote_addr or "?"
    if is_local():
        return
    now = time.time()
    with _lock:
        d = DEVICES.get(ip)
        if not d:
            name, kind = device_label(request.headers.get("User-Agent", ""))
            d = DEVICES[ip] = {"ip": ip, "name": name, "kind": kind,
                               "first_seen": now, "downloads": 0, "uploads": 0}
            log("connect", f"{name} connected", ip=ip)
        d["last_seen"] = now


def log(kind: str, text: str, ip: str = "", size: int = 0) -> None:
    with _lock:
        ACTIVITY.appendleft({"id": uuid.uuid4().hex[:8], "t": time.time(), "kind": kind,
                             "text": text, "ip": ip, "size": size})


# --------------------------------------------------------------------------- #
#  Network helpers                                                             #
# --------------------------------------------------------------------------- #
def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


PORT_CANDIDATES = [80, 8080, 8000, 8765, 8888] + list(range(8766, 8800))


def _port_answers(port: int) -> bool:
    """True if something (another app, IIS/HTTP.sys...) already accepts on this port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def bind_listener() -> socket.socket:
    """Bind the server socket ourselves. Port 80 first so the address has no ':port'.
    SO_EXCLUSIVEADDRUSE stops a second program (or a second Beamly) sharing the port."""
    for port in PORT_CANDIDATES:
        if _port_answers(port):
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
            return s
        except OSError:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    return s


def primary_ip() -> str:
    ips = lan_ips()
    pref = CONFIG.get("preferred_ip")
    return pref if pref in ips else ips[0]


def host_port(ip: str | None = None) -> str:
    port = SERVER["port"]
    return f"{ip or primary_ip()}" + ("" if port == 80 else f":{port}")


def share_url(ip: str | None = None, with_key: bool = True) -> str:
    """Short link for the QR code: http://192.168.1.5/482913"""
    url = f"http://{host_port(ip)}/"
    if with_key and CONFIG["require_pin"]:
        url += CONFIG["pin"]
    return url


# --------------------------------------------------------------------------- #
#  File helpers                                                                #
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def get_share(sid: str) -> dict:
    for s in CONFIG["shares"]:
        if s["id"] == sid:
            return s
    abort(404)


def resolve(sid: str, rel: str = "") -> tuple[dict, Path]:
    """Resolve a path inside a share; refuses anything that escapes it."""
    share = get_share(sid)
    root = Path(share["path"]).resolve()
    if share["type"] == "file":
        if rel and rel != root.name:
            abort(404)
        return share, root
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        abort(403)
    if not target.exists():
        abort(404)
    return share, target


def entry(p: Path) -> dict | None:
    try:
        st = p.stat()
    except OSError:
        return None
    is_dir = p.is_dir()
    mime = None if is_dir else (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
    return {"name": p.name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime, "mime": mime, "ext": p.suffix.lower().lstrip(".")}


def is_hidden(p: Path) -> bool:
    if p.name.startswith(".") or p.name.lower() in ("desktop.ini", "thumbs.db", "$recycle.bin"):
        return True
    try:
        return bool(p.stat().st_file_attributes & 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except (AttributeError, OSError):
        return False


def folder_summary(p: Path) -> tuple[int, int]:
    files, size = 0, 0
    try:
        for f in p.iterdir():
            if f.is_file():
                files += 1
                size += f.stat().st_size
            else:
                files += 1
    except OSError:
        pass
    return files, size


def unique_path(folder: Path, name: str) -> Path:
    name = "".join(c for c in name if c not in '<>:"/\\|?*').strip(" .") or "file"
    target = folder / name
    stem, suffix, i = target.stem, target.suffix, 1
    while target.exists():
        target = folder / f"{stem} ({i}){suffix}"
        i += 1
    return target


class _ZipPipe(io.RawIOBase):
    """Unseekable sink so zipfile streams straight to the HTTP response."""

    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def zip_stream(items: list[tuple[Path, str]]):
    pipe = _ZipPipe()
    with zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for path, arc in items:
            try:
                info = zipfile.ZipInfo.from_file(path, arc, strict_timestamps=False)
                with z.open(info, "w", force_zip64=True) as zf, open(path, "rb") as src:
                    while chunk := src.read(1 << 20):
                        zf.write(chunk)
                        yield pipe.take()
            except OSError:
                continue
            yield pipe.take()
    yield pipe.take()


def walk_files(root: Path, prefix: str) -> list[tuple[Path, str]]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_hidden(Path(dirpath) / d)]
        for fn in filenames:
            p = Path(dirpath) / fn
            if is_hidden(p):
                continue
            out.append((p, f"{prefix}/{p.relative_to(root).as_posix()}"))
    return out


# --------------------------------------------------------------------------- #
#  Flask app                                                                   #
# --------------------------------------------------------------------------- #
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))
app.config["MAX_CONTENT_LENGTH"] = None
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


def is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def authed() -> bool:
    if is_local() or not CONFIG["require_pin"]:
        return True
    return secrets.compare_digest(request.cookies.get(COOKIE, ""), CONFIG["pin"])


@app.before_request
def guard():
    path = request.path
    if path.startswith("/desk") or path.startswith("/admin"):
        if not is_local():
            abort(403)
        return None
    if path.startswith("/static") or path in ("/favicon.ico", "/unlock"):
        return None
    # PIN from the QR code (/482913, or the older ?k=482913) -> set cookie, go to /
    key = path[1:] if len(path) == 7 and path[1:].isdigit() else request.args.get("k")
    if key:
        if not CONFIG["require_pin"] or secrets.compare_digest(key, CONFIG["pin"]):
            resp = make_response(redirect("/"))
            resp.set_cookie(COOKIE, CONFIG["pin"], max_age=60 * 60 * 24 * 30, samesite="Lax")
            return resp
        if path != "/":  # stale QR code after a PIN change -> show the PIN screen
            return redirect("/")
    if not authed():
        if path.startswith("/api") or request.method != "GET" or path != "/":
            return jsonify(error="locked"), 401
        return render_template("unlock.html", app_name=APP_NAME, host=socket.gethostname())
    touch_device()
    return None


@app.post("/unlock")
def unlock():
    ip = request.remote_addr or "?"
    now = time.time()
    with _lock:
        cnt, first = FAILED.get(ip, [0, now])
        if now - first > 300:
            cnt, first = 0, now
        if cnt >= 8:
            return jsonify(ok=False, error="Too many attempts. Wait 5 minutes."), 429
    pin = (request.json or {}).get("pin", "") if request.is_json else request.form.get("pin", "")
    if CONFIG["require_pin"] and not secrets.compare_digest(str(pin).strip(), CONFIG["pin"]):
        with _lock:
            FAILED[ip] = [cnt + 1, first]
        log("warn", "Wrong PIN attempt", ip=ip)
        return jsonify(ok=False, error="Wrong PIN, try again."), 401
    FAILED.pop(ip, None)
    resp = jsonify(ok=True)
    resp.set_cookie(COOKIE, CONFIG["pin"], max_age=60 * 60 * 24 * 30, samesite="Lax")
    return resp


# ---------------- public (phone) side ---------------- #
@app.get("/")
def home():
    return render_template("share.html", app_name=APP_NAME, host=socket.gethostname())


@app.get("/api/info")
def api_info():
    return jsonify(host=socket.gethostname(), allow_upload=CONFIG["allow_upload"],
                   app=APP_NAME, version=VERSION, thumbs=Image is not None)


@app.get("/api/list")
def api_list():
    sid = request.args.get("s", "")
    rel = request.args.get("p", "").strip("/")
    if not sid:
        out = []
        for s in CONFIG["shares"]:
            p = Path(s["path"])
            e = entry(p)
            if not e:
                continue
            e.update(id=s["id"], name=s["name"])
            if e["dir"]:
                e["count"], e["size"] = folder_summary(p)
            out.append(e)
        return jsonify(items=out, crumbs=[])
    share, target = resolve(sid, rel)
    if share["type"] == "file":
        e = entry(target)
        return jsonify(items=[e] if e else [], crumbs=[])
    items = []
    try:
        for p in target.iterdir():
            if is_hidden(p):
                continue
            e = entry(p)
            if e:
                items.append(e)
    except PermissionError:
        abort(403)
    items.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    crumbs = [{"name": share["name"], "p": ""}]
    acc = []
    for part in [x for x in rel.split("/") if x]:
        acc.append(part)
        crumbs.append({"name": part, "p": "/".join(acc)})
    return jsonify(items=items, crumbs=crumbs)


@app.get("/f/<sid>/")
@app.get("/f/<sid>/<path:rel>")
def get_file(sid: str, rel: str = ""):
    share, target = resolve(sid, rel)
    if target.is_dir():
        abort(400)
    as_dl = request.args.get("dl") == "1"
    # only count real downloads, not range-chunks of a preview / video seek
    if as_dl or (not request.headers.get("Range") and request.args.get("pv") != "1"):
        size = target.stat().st_size
        with _lock:
            STATS["downloads"] += 1
            STATS["bytes_out"] += size
            d = DEVICES.get(request.remote_addr)
            if d:
                d["downloads"] += 1
        who = DEVICES.get(request.remote_addr, {}).get("name", request.remote_addr)
        log("download", f"{who} downloaded {target.name}", ip=request.remote_addr, size=size)
    return send_file(target, as_attachment=as_dl, download_name=target.name, conditional=True,
                     max_age=0)


@app.get("/t/<sid>/<path:rel>")
def thumb(sid: str, rel: str):
    if Image is None:
        abort(404)
    _, target = resolve(sid, rel)
    st = target.stat()
    key = f"{target}|{st.st_mtime}|{st.st_size}"
    with _lock:
        data = THUMBS.get(key)
        if data:
            THUMBS.move_to_end(key)
    if data is None:
        try:
            with Image.open(target) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((360, 360))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=78, optimize=True)
                data = buf.getvalue()
        except Exception:
            abort(415)
        with _lock:
            THUMBS[key] = data
            while len(THUMBS) > 600:
                THUMBS.popitem(last=False)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/z/<sid>/")
@app.get("/z/<sid>/<path:rel>")
def get_zip(sid: str, rel: str = ""):
    share, target = resolve(sid, rel)
    if not target.is_dir():
        abort(400)
    items = walk_files(target, target.name)
    total = sum(p.stat().st_size for p, _ in items)
    who = DEVICES.get(request.remote_addr, {}).get("name", request.remote_addr)
    with _lock:
        STATS["downloads"] += 1
        STATS["bytes_out"] += total
    log("zip", f"{who} downloaded folder {target.name} ({len(items)} files)",
        ip=request.remote_addr, size=total)
    resp = Response(zip_stream(items), mimetype="application/zip")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{_quote(target.name)}.zip"
    return resp


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


@app.post("/api/upload")
def upload():
    if not CONFIG["allow_upload"]:
        return jsonify(error="Uploads are turned off on the PC."), 403
    folder = Path(CONFIG["receive_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    who = DEVICES.get(request.remote_addr, {}).get("name", request.remote_addr)
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        dest = unique_path(folder, Path(f.filename).name)
        f.save(dest)
        size = dest.stat().st_size
        saved.append(dest.name)
        with _lock:
            STATS["uploads"] += 1
            STATS["bytes_in"] += size
            d = DEVICES.get(request.remote_addr)
            if d:
                d["uploads"] += 1
        log("upload", f"{who} sent {dest.name}", ip=request.remote_addr, size=size)
    return jsonify(ok=True, saved=saved)


@app.get("/api/messages")
def get_messages():
    return jsonify(items=list(MESSAGES))


@app.post("/api/messages")
def post_message():
    text = ((request.json or {}).get("text") or "").strip()[:20000]
    if not text:
        return jsonify(error="empty"), 400
    who = "PC" if is_local() else DEVICES.get(request.remote_addr, {}).get("name", "Device")
    with _lock:
        MESSAGES.appendleft({"id": uuid.uuid4().hex[:8], "t": time.time(), "text": text,
                             "from": who, "pc": is_local()})
    if not is_local():
        log("text", f"{who} sent a text snippet", ip=request.remote_addr)
    return jsonify(ok=True)


# ---------------- desktop (local only) ---------------- #
@app.get("/desk")
def desk():
    return render_template("desktop.html", app_name=APP_NAME, version=VERSION)


@app.get("/admin/api/state")
def admin_state():
    now = time.time()
    with _lock:
        devices = sorted(DEVICES.values(), key=lambda d: -d["last_seen"])
        devs = [dict(d, online=now - d["last_seen"] < 45) for d in devices]
        shares = []
        for s in CONFIG["shares"]:
            p = Path(s["path"])
            item = dict(s, exists=p.exists())
            if p.is_file():
                item["size"] = p.stat().st_size
            shares.append(item)
        received = []
        rd = Path(CONFIG["receive_dir"])
        if rd.exists():
            try:  # scandir reuses the directory listing's metadata: cheap even with many files
                with os.scandir(rd) as it:
                    files = [(e.name, e.stat(), e.path) for e in it if e.is_file()]
            except OSError:
                files = []
            files.sort(key=lambda f: f[1].st_mtime, reverse=True)
            received = [{"name": n, "size": st.st_size, "mtime": st.st_mtime, "path": p}
                        for n, st, p in files[:40]]
        ips = lan_ips()
        return jsonify(
            ips=ips, ip=primary_ip(), port=SERVER["port"], url=share_url(),
            plain_url=share_url(with_key=False), short=host_port(), pin=CONFIG["pin"],
            require_pin=CONFIG["require_pin"], allow_upload=CONFIG["allow_upload"],
            receive_dir=CONFIG["receive_dir"], shares=shares, devices=devs,
            activity=list(ACTIVITY)[:80], stats=STATS, messages=list(MESSAGES),
            received=received, host=socket.gethostname(),
            online=sum(1 for d in devs if d["online"]), uptime=now - SERVER["started"])


@app.get("/admin/qr.svg")
def admin_qr():
    if qrcode is None:
        abort(500)
    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_Q)
    qr.add_data(share_url())
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathFillImage)
    buf = io.BytesIO()
    img.save(buf)
    resp = Response(buf.getvalue(), mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def add_share(path_str: str) -> dict | None:
    p = Path(path_str).expanduser()
    if not p.exists():
        return None
    p = p.resolve()
    with _lock:
        for s in CONFIG["shares"]:
            if Path(s["path"]) == p:
                return s
        s = {"id": uuid.uuid4().hex[:8], "path": str(p),
             "name": p.name or str(p), "type": "folder" if p.is_dir() else "file",
             "added": time.time()}
        CONFIG["shares"].append(s)
        save_config()
    log("share", f"Now sharing {s['name']}")
    return s


@app.post("/admin/api/shares")
def admin_add_share():
    added = [s for s in (add_share(x) for x in (request.json or {}).get("paths", [])) if s]
    return jsonify(ok=True, added=added)


@app.delete("/admin/api/shares/<sid>")
def admin_remove_share(sid: str):
    with _lock:
        CONFIG["shares"] = [s for s in CONFIG["shares"] if s["id"] != sid]
        save_config()
    return jsonify(ok=True)


@app.delete("/admin/api/shares")
def admin_clear_shares():
    with _lock:
        CONFIG["shares"] = []
        save_config()
    return jsonify(ok=True)


@app.post("/admin/api/settings")
def admin_settings():
    data = request.json or {}
    with _lock:
        for key in ("allow_upload", "require_pin"):
            if key in data:
                CONFIG[key] = bool(data[key])
        if "preferred_ip" in data:
            CONFIG["preferred_ip"] = str(data["preferred_ip"])
        if data.get("receive_dir"):
            CONFIG["receive_dir"] = str(data["receive_dir"])
        if data.get("new_pin"):
            CONFIG["pin"] = _new_pin()
            log("warn", "PIN changed - devices must scan the new QR code")
        save_config()
    return jsonify(ok=True)


@app.post("/admin/api/clear")
def admin_clear():
    what = (request.json or {}).get("what")
    with _lock:
        if what == "activity":
            ACTIVITY.clear()
        elif what == "messages":
            MESSAGES.clear()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
#  pywebview bridge                                                            #
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self):
        # underscore: pywebview must NOT walk into the Window object when it exposes
        # this API to the page - doing so runs JS on the UI thread and freezes the app
        self._window = None

    def pick_files(self):
        import webview
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        return [add_share(p) for p in (res or [])]

    def pick_folder(self):
        import webview
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return [add_share(p) for p in (res or [])]

    def pick_receive_dir(self):
        import webview
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            with _lock:
                CONFIG["receive_dir"] = res[0]
                save_config()
            return res[0]
        return None

    def open_path(self, path: str, reveal: bool = False):
        p = Path(path)
        if not p.exists():
            if reveal:
                p.mkdir(parents=True, exist_ok=True)
            else:
                return False
        if reveal and p.is_file():
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))  # noqa: S606 - local desktop action
        return True

    def open_in_browser(self, url: str):
        import webbrowser
        webbrowser.open(url)
        return True


def run_server(sock: socket.socket) -> None:
    from waitress import serve
    try:
        serve(app, sockets=[sock], threads=24, channel_timeout=300,
              max_request_body_size=1 << 40, connection_limit=200, ident=APP_NAME,
              _quiet=True)
    except Exception:
        import logging
        logging.getLogger(APP_NAME).exception("server stopped")


# --------------------------------------------------------------------------- #
#  Crash log, single instance, window icon                                     #
# --------------------------------------------------------------------------- #
ICON_FILE = BASE_DIR / "static" / "beamly.ico"
WINDOW_TITLE = f"{APP_NAME} - Share files over Wi-Fi"
_MUTEX = None


def setup_logging() -> None:
    """pythonw has no console: send errors and hard-crash tracebacks to beamly.log."""
    import faulthandler
    import logging
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    log_file = CONF_DIR / "beamly.log"
    try:
        if log_file.exists() and log_file.stat().st_size > 2_000_000:
            log_file.unlink()
    except OSError:
        pass
    stream = open(log_file, "a", encoding="utf-8", buffering=1)
    logging.basicConfig(stream=stream, level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    faulthandler.enable(stream)
    sys.excepthook = lambda *e: logging.getLogger(APP_NAME).error("uncaught", exc_info=e)
    threading.excepthook = lambda a: logging.getLogger(APP_NAME).error(
        "thread %s crashed", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    logging.getLogger(APP_NAME).warning("Beamly %s starting (pid %s)", VERSION, os.getpid())


def already_running() -> bool:
    """If Beamly is open already, bring that window to the front instead of starting again."""
    global _MUTEX
    if os.name != "nt":
        return False
    import ctypes
    k32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX = k32.CreateMutexW(None, False, "Local\\SharpCodeIT.Beamly.Desktop")
    if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return False
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    return True


def _set_app_id() -> None:
    """Own taskbar identity, so Windows shows Beamly's icon instead of Python's."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SharpCodeIT.Beamly")
        except Exception:
            pass


def _apply_window_icon() -> None:
    """Runs on a worker thread. PostMessage never waits on the UI thread, so it cannot freeze it."""
    if os.name != "nt" or not ICON_FILE.exists():
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    hwnd = None
    for _ in range(50):
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
    big = user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
    small = user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
    if big:
        user32.PostMessageW(hwnd, WM_SETICON, 1, big)
    if small:
        user32.PostMessageW(hwnd, WM_SETICON, 0, small)


def main() -> None:
    server_only = "--server" in sys.argv
    if not server_only:
        setup_logging()
        if already_running():
            return

    sock = bind_listener()
    port = sock.getsockname()[1]
    SERVER["port"] = port
    threading.Thread(target=run_server, args=(sock,), daemon=True, name="waitress").start()
    Path(CONFIG["receive_dir"]).mkdir(parents=True, exist_ok=True)
    log("info", f"Beamly is live on {share_url(with_key=False)}")

    for _ in range(50):  # wait until the server answers
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    if server_only:
        print(f"{APP_NAME} running at {share_url()}  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    import webview
    _set_app_id()
    api = Api()
    window = webview.create_window(
        WINDOW_TITLE, f"http://127.0.0.1:{port}/desk",
        js_api=api, width=1240, height=800, min_size=(1000, 660),
        background_color="#F4F6FB")
    api._window = window

    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        added = [add_share(f["pywebviewFullPath"]) for f in files if f.get("pywebviewFullPath")]
        if any(added):
            try:
                window.evaluate_js("window.refresh && window.refresh(true)")
            except Exception:
                pass

    dnd_ready = []

    def on_loaded():
        if dnd_ready:  # the page can reload; register the drop handler only once
            return
        dnd_ready.append(True)
        # drag & drop from Explorer. Only "drop" goes to Python; the page itself
        # cancels dragover, so moving the mouse during a drag costs nothing here.
        try:
            from webview.dom import DOMEventHandler
            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            import logging
            logging.getLogger(APP_NAME).exception("drag & drop unavailable")

    window.events.loaded += on_loaded
    window.events.shown += _apply_window_icon
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()
