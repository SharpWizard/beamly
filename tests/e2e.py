from pathlib import Path
import os, sys, json, time, subprocess, pathlib, tempfile, zipfile, urllib.request
from PIL import Image

OUT = pathlib.Path(__file__).parent / "shots"; OUT.mkdir(exist_ok=True)
appdata = pathlib.Path(tempfile.mkdtemp()); (appdata / "Beamly").mkdir()
data = pathlib.Path(tempfile.mkdtemp()) / "Holiday Photos"; (data / "Docs").mkdir(parents=True)
for i, c in enumerate(["#4F5BFF", "#E0445E", "#12A779", "#F5A524"]):
    Image.new("RGB", (1200, 900), c).save(data / f"photo_{i + 1}.jpg")
(data / "Docs" / "notes.txt").write_text("Hello from Beamly test")
(data / "readme.md").write_text("# hi")
single = data.parent / "Report 2026.pdf"; single.write_bytes(b"%PDF-1.4\n%fake\n")
recv = pathlib.Path(tempfile.mkdtemp())
(appdata / "Beamly" / "config.json").write_text(json.dumps({"port": 8799, "pin": "482913", "receive_dir": str(recv), "shares": []}))
env = dict(os.environ, APPDATA=str(appdata))
srv = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve().parent.parent / "app.py"), "--server"], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
line = srv.stdout.readline()          # "Beamly running at http://IP[:port]/PIN  (Ctrl+C to stop)"
QR_URL = line.split(" at ")[1].split()[0]
_hp = QR_URL.split("/")[2]
PORT = int(_hp.split(":")[1]) if ":" in _hp else 80
BASE = f"http://127.0.0.1:{PORT}"
print("server:", QR_URL)


def admin(path, body=None, method="POST"):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def state():
    return json.loads(urllib.request.urlopen(BASE + "/admin/api/state").read())


admin("/admin/api/shares", {"paths": [str(data), str(single)]})
PH = "http://" + state()["short"]
results, errors = [], []


def check(name, cond):
    results.append(("PASS" if cond else "FAIL", name))


from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        # ---------------- PHONE ----------------
        ph = b.new_context(**p.devices["Pixel 7"], accept_downloads=True)
        pg = ph.new_page()
        pg.on("console", lambda m: m.type == "error" and errors.append("phone: " + m.text))
        pg.on("pageerror", lambda e: errors.append("phone pageerror: " + str(e)))
        pg.goto(PH + "/"); pg.wait_for_timeout(1000)
        check("unlock screen shown to new phone", pg.locator("#boxes").is_visible())
        pg.screenshot(path=str(OUT / "1_unlock.png"))
        pg.locator("#pin").fill("111111"); pg.wait_for_timeout(800)
        check("wrong PIN shows error", "Wrong PIN" in pg.locator("#msg").inner_text())
        pg.wait_for_timeout(500); pg.locator("#pin").fill("482913"); pg.wait_for_timeout(400)
        check("right PIN shows success state", pg.locator("#card.ok").count() == 1)
        pg.wait_for_selector("#files .row", timeout=6000); pg.wait_for_timeout(900)
        names = pg.locator("#files .row .meta b").all_inner_texts()
        check("home lists both shares", names == ["Holiday Photos", "Report 2026.pdf"])
        pg.screenshot(path=str(OUT / "2_home.png"))
        pg.locator("#files .row").first.click(); pg.wait_for_timeout(1300)
        check("folder opens with 6 items", pg.locator("#files .row").count() == 6)
        check("thumbnails load", pg.locator("#files .fi img.ok").count() == 4)
        check("breadcrumb shows folder", "Holiday Photos" in pg.locator("#crumbs").inner_text())
        pg.screenshot(path=str(OUT / "3_folder.png"))
        pg.locator("#q").fill("photo_3"); pg.wait_for_timeout(200)
        check("search filters", pg.locator("#files .row").count() == 1)
        pg.locator("#q").fill(""); pg.wait_for_timeout(200)
        pg.locator("#viewBtn").click(); pg.wait_for_timeout(1000)
        check("grid view toggles", pg.locator("#files .tile").count() == 6)
        pg.screenshot(path=str(OUT / "4_grid.png"))
        pg.locator("#files .tile .th").nth(2).click(); pg.wait_for_timeout(900)
        check("lightbox opens image", pg.locator("#lbBody img").count() == 1 and pg.locator("#lb.on").count() == 1)
        first = pg.locator("#lbName").inner_text(); pg.screenshot(path=str(OUT / "5_lightbox.png"))
        pg.locator(".lb-nav.n").click(); pg.wait_for_timeout(500)
        check("lightbox next works", pg.locator("#lbName").inner_text() != first)
        with pg.expect_download() as d:
            pg.locator("#lbDl").click()
        check("lightbox download", d.value.suggested_filename.endswith(".jpg"))
        pg.locator("#lbClose").click(); pg.wait_for_timeout(600)
        check("lightbox closes (back)", pg.locator("#lb.on").count() == 0 and pg.locator("#files .tile").count() == 6)
        pg.locator("#viewBtn").click(); pg.wait_for_timeout(700)
        with pg.expect_download() as d:
            pg.locator("#files .row .dl").first.click()
        check("row download button", d.value.suggested_filename.startswith("photo_"))
        with pg.expect_download() as d:
            pg.locator("#files .sec a.chip").click()
        zp = OUT / "all.zip"; d.value.save_as(zp)
        zf = zipfile.ZipFile(zp)
        check("download all = valid zip of 6 files", len(zf.namelist()) == 6 and zf.testzip() is None)
        zf.close()
        pg.locator("#files .row", has_text="Docs").click(); pg.wait_for_timeout(900)
        check("subfolder opens", pg.locator("#files .row").count() == 1)
        pg.go_back(); pg.wait_for_timeout(900)
        check("browser back returns to folder", pg.locator("#files .row").count() == 6)
        pg.locator("#crumbs .crumb").first.click(); pg.wait_for_timeout(900)
        check("Home crumb returns home", pg.locator("#files .hero").count() == 1)
        # upload
        pg.locator(".nav button[data-v=send]").click(); pg.wait_for_timeout(500)
        upd = pathlib.Path(tempfile.mkdtemp())
        up = upd / "from phone.jpg"; Image.new("RGB", (640, 480), "#8B5CF6").save(up)
        big = upd / "video clip.mp4"; big.write_bytes(os.urandom(6 * 1024 * 1024))
        pg.locator("#fileIn").set_input_files([str(up), str(big)])
        pg.wait_for_function("document.querySelectorAll('.up.done').length===2", timeout=20000); pg.wait_for_timeout(400)
        check("2 uploads finish", pg.locator(".up.done").count() == 2)
        check("files saved on PC", sorted(x.name for x in recv.iterdir()) == ["from phone.jpg", "video clip.mp4"])
        pg.screenshot(path=str(OUT / "6_send.png"))
        # clipboard
        pg.locator(".nav button[data-v=clip]").click(); pg.wait_for_timeout(400)
        pg.locator("#msgText").fill("https://example.com OTP 5521"); pg.locator("#sendBtn").click(); pg.wait_for_timeout(1000)
        check("phone sends text", pg.locator("#msgs .msg").count() == 1 and pg.locator("#msgs a.lnk").count() == 1)
        pg.locator(".nav button[data-v=files]").click()
        # ---------------- DESKTOP ----------------
        dk = b.new_context(viewport={"width": 1280, "height": 820}); dg = dk.new_page()
        dg.on("console", lambda m: m.type == "error" and errors.append("desk: " + m.text))
        dg.on("pageerror", lambda e: errors.append("desk pageerror: " + str(e)))
        dg.goto(BASE + "/desk"); dg.wait_for_timeout(2500)
        check("desk shows 2 shares", dg.locator("#shareList .item").count() == 2)
        check("desk counts device online", dg.locator("#sDevices").inner_text() == "1")
        check("desk counts uploads", dg.locator("#sUp").inner_text() == "2")
        check("QR image loaded", dg.evaluate("document.querySelector('#qr').naturalWidth>0"))
        check("PIN digits shown", dg.locator("#pin span").all_inner_texts() == list("482913"))
        check("activity filled", dg.locator("#actList .item").count() >= 5)
        check("relative times filled", dg.locator("#actList [data-ago]").first.inner_text() != "")
        dg.screenshot(path=str(OUT / "7_desk.png"))
        for t in ["devices", "received", "clipboard", "settings", "activity"]:
            dg.locator(f".tab[data-t={t}]").click(); dg.wait_for_timeout(500)
            check(f"tab {t} shows pane", dg.locator(f"#p-{t}.on").count() == 1)
            if t == "devices":
                txt = dg.locator("#devList").inner_text()
                check("device row = Android phone online", "Android phone" in txt and "Online" in txt)
                dg.screenshot(path=str(OUT / "8_devices.png"))
            if t == "received":
                check("received lists 2 files", dg.locator("#recvList .item").count() == 2)
            if t == "clipboard":
                check("phone text visible on desk", "OTP 5521" in dg.locator("#msgList").inner_text())
                dg.locator("#msgText").fill("Hello phone from PC"); dg.locator("#msgText").press("Enter"); dg.wait_for_timeout(1900)
                check("desk sends text", dg.locator("#msgList .msg").count() == 2)
                dg.screenshot(path=str(OUT / "9_clip.png"))
            if t == "settings":
                dg.screenshot(path=str(OUT / "10_settings.png"))
                dg.locator("[data-k=allow_upload]").click(); dg.wait_for_timeout(1800)
                check("toggle uploads off", state()["allow_upload"] is False)
                dg.locator("[data-k=allow_upload]").click(); dg.wait_for_timeout(1800)
                check("toggle uploads back on", state()["allow_upload"] is True)
        check("tab ink indicator sized", dg.evaluate("parseFloat(getComputedStyle(document.querySelector('#ink')).width)") > 20)
        pg.wait_for_timeout(4500)
        check("phone shows new-message badge", pg.locator("#msgBadge").is_visible())
        pg.locator(".nav button[data-v=clip]").click(); pg.wait_for_timeout(800)
        check("phone sees desk text", "Hello phone" in pg.locator("#msgs").inner_text())
        pg.screenshot(path=str(OUT / "11_phone_clip.png"))
        dg.locator("#shareList .item").nth(1).hover(); dg.wait_for_timeout(400)
        dg.locator("#shareList .item").nth(1).locator("[data-act=unshare]").click(); dg.wait_for_timeout(1900)
        check("unshare removes row on desk", dg.locator("#shareList .item").count() == 1)
        pg.locator(".nav button[data-v=files]").click(); pg.wait_for_timeout(5800)
        check("phone home auto-updates after unshare", pg.locator("#files .row").count() == 1)
        dg.locator(".tab[data-t=settings]").click(); dg.wait_for_timeout(300)
        old = dg.evaluate("document.querySelector('#qr').src")
        dg.locator("[data-act=new-pin]").click(); dg.wait_for_timeout(1900)
        check("new PIN changes QR", dg.evaluate("document.querySelector('#qr').src") != old)
        check("old cookie rejected after new PIN", pg.request.get(PH + "/api/list").status == 401)
        # short QR link logs a fresh phone straight in; a stale one falls back to the PIN screen
        ph2 = b.new_context(**p.devices["Pixel 7"]); pg2 = ph2.new_page()
        pg2.goto(state()["url"]); pg2.wait_for_timeout(1200)
        check("short QR link /PIN logs in", pg2.locator("#files .row").count() >= 1)
        ph3 = b.new_context(**p.devices["Pixel 7"]); pg3 = ph3.new_page()
        pg3.goto(PH + "/000001"); pg3.wait_for_timeout(800)
        check("stale QR link shows PIN screen", pg3.locator("#boxes").count() == 1)
        check("QR link is short", len(state()["url"]) <= len("http://192.168.100.100:65535/123456"))
        check("admin blocked from LAN", pg.request.get(PH + "/admin/api/state").status == 403)
        b.close()
finally:
    srv.terminate()

for s, n in results:
    print(s, n)
print("\nconsole errors:", errors or "none")
print(f"\n{sum(1 for s, _ in results if s == 'PASS')}/{len(results)} passed")
