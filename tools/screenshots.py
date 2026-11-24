"""Render README screenshots with neutral demo data (no real host name, user paths or IPs).

    python tools/screenshots.py        ->  docs/screenshots/*.png
Needs: pip install playwright && python -m playwright install chromium
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
DEMO = Path(tempfile.mkdtemp(prefix="beamly-demo-"))   # private temp dir, removed afterwards
SHOWN_DIR = r"D:\Media"                                 # what the screenshots display instead
FAKE_HOST, FAKE_IP, PHONE_IP = "Studio-PC", "192.168.1.20", "192.168.1.42"

os.environ["APPDATA"] = tempfile.mkdtemp()           # isolated config, before importing app
sys.path.insert(0, str(ROOT))
import app as A                                        # noqa: E402
from PIL import Image, ImageDraw, ImageFilter           # noqa: E402

A.socket.gethostname = lambda: FAKE_HOST
A.lan_ips = lambda: [FAKE_IP]


class FakeRemote:
    """Show phones as 192.168.1.42 and keep the desktop local."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, env, start):
        if env.get("HTTP_X_DEMO_PHONE"):
            env["REMOTE_ADDR"] = PHONE_IP
        return self.wsgi(env, start)


A.app.wsgi_app = FakeRemote(A.app.wsgi_app)


def photo(path, c1, c2, seed):
    w, h = 1600, 1200
    img = Image.new("RGB", (w, h), c1)
    d = ImageDraw.Draw(img)
    for i in range(h):
        t = i / h
        d.line([(0, i), (w, i)], fill=tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3)))
    d.ellipse((w * .58, h * .12, w * .78, h * .38), fill=(255, 244, 214))
    for j, col in enumerate([(60, 70, 120), (40, 50, 95), (25, 32, 70)]):
        off = j * 110 + seed * 37
        d.polygon([(0, h), (w * .2 + off, h * .45 + j * 90), (w * .5 + off, h * .7), (w * .8 - off, h * .5 + j * 70), (w, h)], fill=col)
    img.filter(ImageFilter.GaussianBlur(1)).save(path, quality=88)


def build_demo():
    shutil.rmtree(DEMO, ignore_errors=True)
    trip = DEMO / "Cox's Bazar Trip"
    (trip / "Videos").mkdir(parents=True)
    pal = [((255, 154, 98), (120, 72, 160)), ((86, 204, 242), (47, 128, 237)), ((247, 151, 30), (255, 210, 0)),
           ((17, 153, 142), (56, 239, 125)), ((238, 9, 121), (255, 106, 0)), ((131, 96, 195), (46, 191, 145))]
    for i, (a, b) in enumerate(pal):
        photo(trip / f"IMG_20{41 + i}.jpg", a, b, i)
    (trip / "Videos" / "sunset.mp4").write_bytes(os.urandom(2_400_000))
    (trip / "Itinerary.pdf").write_bytes(b"%PDF-1.4\n" + os.urandom(180_000))
    (trip / "Budget.xlsx").write_bytes(os.urandom(24_000))
    work = DEMO / "Work"
    work.mkdir()
    (work / "Proposal v3.docx").write_bytes(os.urandom(64_000))
    (work / "Logo pack.zip").write_bytes(os.urandom(3_100_000))
    (DEMO / "Song - Live.mp3").write_bytes(os.urandom(4_200_000))
    (DEMO / "Beamly-Setup.exe").write_bytes(os.urandom(5_600_000))
    return [trip, work, DEMO / "Song - Live.mp3", DEMO / "Beamly-Setup.exe"]


def main():
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    recv = Path(tempfile.mkdtemp())
    A.CONFIG.update(receive_dir=str(recv), pin="482913", require_pin=True, allow_upload=True, shares=[])
    for p in build_demo():
        A.add_share(str(p))
    sock = A.bind_listener()
    A.SERVER["port"] = port = sock.getsockname()[1]
    threading.Thread(target=A.run_server, args=(sock,), daemon=True).start()
    time.sleep(1)
    local = f"http://127.0.0.1:{port}"
    phone_hdr = {"X-Demo-Phone": "1"}

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ph = b.new_context(**pw.devices["Pixel 7"], extra_http_headers=phone_hdr)
        pg = ph.new_page()
        pg.goto(local + "/"); pg.wait_for_timeout(1500)
        pg.locator("#pin").fill("4829"); pg.wait_for_timeout(500)
        pg.screenshot(path=str(OUT / "phone-pin.png"))
        pg.goto(local + "/482913"); pg.wait_for_timeout(1800)
        pg.screenshot(path=str(OUT / "phone-home.png"))
        pg.locator("#files .row").first.click(); pg.wait_for_timeout(1800)
        if pg.locator("#files .tile").count() == 0:
            pg.locator("#viewBtn").click(); pg.wait_for_timeout(1800)
        pg.screenshot(path=str(OUT / "phone-grid.png"))
        pg.locator("#files .tile .th").nth(2).click(); pg.wait_for_timeout(1400)
        pg.screenshot(path=str(OUT / "phone-preview.png"))
        pg.go_back(); pg.wait_for_timeout(600)
        pg.locator("#viewBtn").click(); pg.wait_for_timeout(1500)
        pg.screenshot(path=str(OUT / "phone-list.png"))
        pg.request.get(local + "/f/" + A.CONFIG["shares"][0]["id"] + "/IMG_2041.jpg?dl=1", headers=phone_hdr)
        # send two files to the PC
        pg.locator(".nav button[data-v=send]").click(); pg.wait_for_timeout(500)
        tmp = Path(tempfile.mkdtemp())
        photo(tmp / "Receipt.jpg", (230, 230, 240), (180, 190, 230), 3)
        (tmp / "Voice note.m4a").write_bytes(os.urandom(1_500_000))
        pg.locator("#fileIn").set_input_files([str(tmp / "Receipt.jpg"), str(tmp / "Voice note.m4a")])
        pg.wait_for_function("document.querySelectorAll('.up.done').length===2", timeout=20000)
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(OUT / "phone-send.png"))
        pg.locator(".nav button[data-v=clip]").click(); pg.wait_for_timeout(400)
        pg.locator("#msgText").fill("Hotel booking ref: BX-20417\nhttps://maps.example.com/hotel")
        pg.locator("#sendBtn").click(); pg.wait_for_timeout(800)

        dk = b.new_context(viewport={"width": 1280, "height": 820}, device_scale_factor=1.5)
        dg = dk.new_page()

        def neutral_paths(route):
            """Show demo paths as D:\Media\... so no real user folder appears in the README."""
            resp = route.fetch()
            data = resp.json()

            def fix(v):
                if isinstance(v, str):
                    return v.replace(str(DEMO), SHOWN_DIR).replace(str(recv), SHOWN_DIR + r"\Received")
                if isinstance(v, list):
                    return [fix(x) for x in v]
                if isinstance(v, dict):
                    return {k: fix(x) for k, x in v.items()}
                return v
            route.fulfill(response=resp, json=fix(data))

        dg.route("**/admin/api/state", neutral_paths)
        dg.goto(local + "/desk"); dg.wait_for_timeout(3000)
        dg.screenshot(path=str(OUT / "desktop.png"))
        dg.locator(".tab[data-t=clipboard]").click()
        dg.locator("#msgText").fill("Wi-Fi password for guests: sunrise-2041"); dg.locator("#msgText").press("Enter")
        dg.wait_for_timeout(2500)
        dg.screenshot(path=str(OUT / "desktop-clipboard.png"))
        pg.wait_for_timeout(4500)
        pg.screenshot(path=str(OUT / "phone-clipboard.png"))
        b.close()
    shutil.rmtree(DEMO, ignore_errors=True)
    print("screenshots in", OUT)


if __name__ == "__main__":
    main()
