r"""Synthwave sunset brand kit for the episode stings (copy of the WD1/WD2/Legion series kit).

Ports the look of the channel's old stream-starting overlay to a frame renderer, so the
YouTube intro/outro match the published series. Fonts: the Windows originals when present,
else the bundled/Linux stand-ins (FONT_SUBS), so the same code runs on a cloud GPU box.
Palette, sun geometry, horizon position and grid colours are taken from that CSS/canvas source.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1920, 1080
HY = 628                      # horizon y, from the source
SUN_C = (960, 500)
SUN_R = 320
import os

FONTS = r"C:\Windows\Fonts"
_REPO_FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "fonts")
FONT_SUBS = {   # Windows font -> stand-in on Linux (Nunito Black is in the repo; DejaVu = fonts-dejavu-core)
    "ariblk.ttf": [os.path.join(_REPO_FONTS, "Nunito-Black.ttf")],
    "arial.ttf": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "arialbd.ttf": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}

MINT = [(0.00, (0xa0, 0xff, 0xe8)), (0.30, (0x7f, 0xff, 0xd4)),
        (0.70, (0x40, 0xe8, 0xb0)), (1.00, (0x20, 0xc0, 0x90))]
KICK_GREEN = (0x53, 0xFC, 0x18)


def _stops(stops, t):
    t = np.clip(t, 0, 1)
    pos = np.array([s[0] for s in stops])
    cols = np.array([s[1] for s in stops], dtype=np.float32)
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    for c in range(3):
        out[..., c] = np.interp(t, pos, cols[:, c])
    return out


def _bg_static(seed=7):
    """Sky, sun, horizon, floor — everything that doesn't move."""
    img = np.zeros((H, W, 3), dtype=np.float32)
    y = np.arange(H)[:, None]

    sky = _stops([(0.00, (0x04, 0x08, 0x0f)), (0.45, (0x0c, 0x16, 0x28)),
                  (0.75, (0x16, 0x0c, 0x28)), (1.00, (0x24, 0x0e, 0x3c))],
                 (np.arange(HY) / HY))
    img[:HY] = sky[:, None, :]

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)

    # stars go down BEFORE the sun so the sun occludes them (as in the source)
    rng = np.random.default_rng(seed)
    stars = []
    for _ in range(200):
        x = int(rng.integers(0, W)); yv = int(rng.integers(0, int(HY * 0.9)))
        a = 0.3 + float(rng.random()) * 0.7
        r = float(rng.random()) * 1.6 + 0.2
        stars.append((x, yv, a, r, float(rng.random()) * 6.28))
        rr = int(np.ceil(r)) + 1
        y0, y1 = max(0, yv - rr), min(HY, yv + rr + 1)
        x0, x1 = max(0, x - rr), min(W, x + rr + 1)
        if y1 <= y0 or x1 <= x0:
            continue
        d = np.sqrt((np.arange(x0, x1)[None, :] - x) ** 2 +
                    (np.arange(y0, y1)[:, None] - yv) ** 2)
        img[y0:y1, x0:x1] += (np.clip(1.0 - d / max(r, 0.6), 0, 1) * a * 255)[..., None]

    # sun glow (additive)
    dg = np.sqrt((xx - SUN_C[0]) ** 2 + (yy - SUN_C[1]) ** 2)
    gt = np.clip((dg - SUN_R * 0.5) / (SUN_R * 1.6 - SUN_R * 0.5), 0, 1)
    ga = np.interp(gt, [0, 0.4, 1], [0.40, 0.15, 0.0])
    img += ga[..., None] * np.array([255, 100, 10], dtype=np.float32)

    # sun body: radial gradient offset upward, as in the source
    dd = np.sqrt((xx - SUN_C[0]) ** 2 + (yy - (SUN_C[1] - 60)) ** 2)
    st = np.clip(dd / SUN_R, 0, 1)
    body = _stops([(0.00, (0xff, 0xe0, 0x80)), (0.25, (0xff, 0xaa, 0x00)),
                   (0.55, (0xff, 0x66, 0x00)), (0.85, (0xdd, 0x33, 0x00)),
                   (1.00, (0xaa, 0x18, 0x00))], st)
    circ = (np.sqrt((xx - SUN_C[0]) ** 2 + (yy - SUN_C[1]) ** 2) <= SUN_R)
    img[circ] = body[circ]

    # retro scanline slits across the sun
    for f in (0.56, 0.63, 0.695, 0.755, 0.815, 0.875, 0.935):
        sy = SUN_C[1] - SUN_R + f * SUN_R * 2
        band = (yy >= sy) & (yy < sy + SUN_R * 0.042) & circ
        img[band] *= (1 - 0.42)

    # horizon glow + hard line
    hg = (yy >= HY - 80) & (yy <= HY + 60)
    ht = np.clip((yy - (HY - 80)) / 140.0, 0, 1)
    ha = np.interp(ht, [0, 0.6, 0.8, 1.0], [0, 0.28, 0.55, 0.10])
    img[hg] += (ha[hg])[..., None] * np.array([255, 110, 0], dtype=np.float32)
    lx = np.clip(np.abs(np.arange(W) - W / 2) / (W / 2), 0, 1)
    la = np.interp(lx, [0, 0.35, 0.7, 1.0], [1.0, 0.9, 0.9, 0.0])
    img[HY - 1:HY + 2] += (la[None, :, None] * np.array([255, 200, 50], dtype=np.float32)) * 0.9

    # floor LAST so it occludes the lower half of the sun, as the source does
    flr = _stops([(0.0, (0x08, 0x03, 0x1e)), (1.0, (0x0e, 0x05, 0x28))],
                 (np.arange(H - HY) / (H - HY)))
    img[HY:] = flr[:, None, :]

    return np.clip(img, 0, 255), stars


_BG_CACHE = None


def background(grid_offset=0.0, twinkle=0.0):
    """Full animated background: static plate + scrolling perspective grid."""
    global _BG_CACHE
    if _BG_CACHE is None:
        _BG_CACHE = _bg_static()
    base, stars = _BG_CACHE
    img = Image.fromarray(base.astype(np.uint8), "RGB")

    if twinkle:
        d = ImageDraw.Draw(img, "RGBA")
        for x, yv, a, r, ph in stars:
            v = 0.5 + 0.5 * np.sin(ph + twinkle)
            if v < 0.55:
                continue
            al = int(min(1.0, (v - 0.55) * 1.2) * 255)
            rr = r * 1.3
            d.ellipse([x - rr, yv - rr, x + rr, yv + rr], fill=(255, 255, 255, al))

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    ROWS, COLS = 20, 30
    FLOOR = H - HY
    for i in range(ROWS + 1):
        t = ((i / ROWS) + grid_offset) % 1.0
        yv = HY + t * FLOOR
        a = int(min(1.0, t * 1.4) * 0.7 * 255)
        d.line([(0, yv), (W, yv)], fill=(210, 90, 255, a), width=max(1, int(0.5 + t * 1.2)))
    for i in range(COLS + 1):
        t = i / COLS
        a = int((0.12 + 0.5 * (1 - abs(t * 2 - 1))) * 255)
        d.line([(W / 2, HY), (t * W, H + 20)], fill=(230, 100, 255, a), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img


# ----------------------------------------------------------------------------- text
def font(name, size):
    for p in [os.path.join(FONTS, name)] + FONT_SUBS.get(name, []):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    raise SystemExit("font %s not found (looked in %s and %s)" % (name, FONTS, FONT_SUBS.get(name)))


def _text_mask(text, fnt, tracking=0):
    """Render text to an L mask, honouring letter-spacing (PIL has no tracking)."""
    widths = []
    for ch in text:
        widths.append(fnt.getlength(ch))
    total = int(sum(widths) + tracking * max(0, len(text) - 1)) + 8
    asc, desc = fnt.getmetrics()
    hgt = asc + desc + 8
    m = Image.new("L", (max(total, 1), hgt), 0)
    d = ImageDraw.Draw(m)
    x = 4.0
    for ch, w in zip(text, widths):
        d.text((x, 4), ch, font=fnt, fill=255)
        x += w + tracking
    return m


def gradient_text(img, text, fnt, cx, top, stops=MINT, tracking=4,
                  glow=(127, 255, 212), glow_r=8, glow_a=0.55,
                  hard_shadows=((4, 4, (5, 30, 25), 0.9), (8, 8, (2, 15, 12), 0.7)),
                  align="center"):
    """Mint gradient fill + offset hard shadows + outer glow, as in the overlay CSS."""
    m = _text_mask(text, fnt, tracking)
    mw, mh = m.size
    x = int(cx - mw / 2) if align == "center" else int(cx)

    for dx, dy, col, al in hard_shadows:
        sh = Image.new("RGBA", (mw, mh), col + (0,))
        sh.putalpha(m.point(lambda v: int(v * al)))
        img.alpha_composite(sh, (x + dx, top + dy))

    if glow_r:
        # tight rim glow that hugs the letters. A wide/boosted blur hazes out into the
        # text's bounding rectangle, which reads as an ugly glowing "box" on dark bgs.
        g = m.filter(ImageFilter.GaussianBlur(glow_r))
        gl = Image.new("RGBA", (mw, mh), glow + (0,))
        gl.putalpha(g.point(lambda v: int(min(255, v * glow_a))))
        img.alpha_composite(gl, (x, top))

    grad = _stops(stops, (np.arange(mh) / max(mh - 1, 1)))
    fill = Image.fromarray(np.repeat(grad[:, None, :], mw, axis=1).astype(np.uint8), "RGB").convert("RGBA")
    fill.putalpha(m)
    img.alpha_composite(fill, (x, top))
    return mw, mh


def plain_text(img, text, fnt, cx, top, color=(180, 240, 225), alpha=0.65,
               tracking=10, align="center"):
    m = _text_mask(text, fnt, tracking)
    mw, mh = m.size
    x = int(cx - mw / 2) if align == "center" else int(cx)
    layer = Image.new("RGBA", (mw, mh), tuple(color) + (0,))
    layer.putalpha(m.point(lambda v: int(v * alpha)))
    img.alpha_composite(layer, (x, top))
    return mw, mh
