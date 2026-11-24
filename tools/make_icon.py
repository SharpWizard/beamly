"""Render the Beamly logo to static/beamly.ico (16-256 px) and static/beamly.png (512 px)."""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
S = 1024                       # master canvas
C1, C2 = (79, 91, 255), (139, 92, 246)


def gradient(size):
    g = Image.linear_gradient("L").rotate(45, expand=True)
    w, h = g.size
    g = g.crop((w / 2 - 90, h / 2 - 90, w / 2 + 90, h / 2 + 90)).resize((size, size), Image.LANCZOS)
    return Image.composite(Image.new("RGB", (size, size), C2), Image.new("RGB", (size, size), C1), g)


def render(bold=1.0):
    k = S / 64                                  # logo is drawn on a 64-unit grid
    pad = 2 * k                                 # room for the soft shadow
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # soft drop shadow
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + k, S - pad, S - pad + k * .6), radius=17 * k, fill=110)
    img.paste((40, 40, 120, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(k * 1.2)))

    # gradient tile
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, S - pad, S - pad), radius=17 * k, fill=255)
    img.paste(gradient(S), (0, 0), mask)

    # glossy top highlight
    hl = Image.new("L", (S, S), 0)
    ImageDraw.Draw(hl).ellipse((-S * .3, -S * .75, S * 1.3, S * .45), fill=38)
    hl = ImageChops.multiply(hl.filter(ImageFilter.GaussianBlur(k * 3)), mask)
    img.paste((255, 255, 255, 255), (0, 0), hl)

    # beam waves + stem + dot (white, round caps)
    d = ImageDraw.Draw(img)
    cx, cy = 32 * k, 36 * k
    w = 4.6 * k * bold
    for r in (18, 10):
        r *= k
        d.arc((cx - r, cy - r, cx + r, cy + r), 180, 360, fill="white", width=int(w))
        for x in (cx - r + w / 2, cx + r - w / 2):
            d.ellipse((x - w / 2, cy - w / 2 + 1, x + w / 2, cy + w / 2 + 1), fill="white")
    d.line((cx, cy, cx, 52 * k), fill="white", width=int(w))
    d.ellipse((cx - w / 2, 52 * k - w / 2, cx + w / 2, 52 * k + w / 2), fill="white")
    dot = 4.2 * k * bold
    d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill="white")
    return img


def main():
    normal, bold = render(), render(bold=1.35)   # thicker strokes stay readable at 16-32 px
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    frames = [(bold if s <= 32 else normal).resize((s, s), Image.LANCZOS) for s in sizes]
    out = ROOT / "static"
    frames[-1].save(out / "beamly.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    normal.resize((512, 512), Image.LANCZOS).save(out / "beamly.png")
    print("wrote", out / "beamly.ico", "and beamly.png")


if __name__ == "__main__":
    main()
