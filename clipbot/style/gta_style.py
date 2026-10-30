"""TwinkiesGhost GTA 6 launch: thumbnail + Shorts cover templates (Vice City pixel/neon, matches the camp overlay).

Pure Python + Pillow (no Chrome, no GPU): a thumbnail costs well under a second of one CPU core, so it can run
while Leon is live and in ClipBot's cloud runner. GTA-INSPIRED ONLY: no Rockstar logos, fonts or trademarks; all
lettering is the camp's own hand-made pixel capitals (glyphs.json, same as obs/title.js).

CLI
  py gta_style.py thumb --frame f.png --title "THE HEIST GOES WRONG" --ep 12 [--series "VICE CITY"] [--variant sunset|neon|split] --out ep12.png
  py gta_style.py cover --frame f.png --title "HE DID NOT SEE THAT" [--variant sunset|neon] --out short.png
  py gta_style.py batch spec.json      spec = [{"kind":"thumb"|"cover", "frame":..., "title":..., "ep":..., "series":..., "variant":..., "out":...}]
Exit 2 = the title cannot fit at the smallest legal size (shorten it; nothing is written). Exit 3 = bad input.

Import
  from gta_style import episode_thumb, short_cover, TextOverflow
"""
import json, os, random, re, sys
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
G = json.load(open(os.path.join(HERE, "glyphs.json"), encoding="utf-8"))

# camp palette (stream-assets/obs/gta.js)
SKY = ['#120a26', '#1b0f3a', '#29144d', '#3c1a5e', '#56216a', '#78296f', '#9d3472', '#c24572', '#e05f6c', '#f27e60', '#fba35d', '#ffc56c']
INK = '#120a26'          # outline / deepest night
PINK, CYAN, ORANGE = '#ff4fd8', '#3ff0ff', '#ff9b3f'
SUN_TOP, SUN_BOT = '#ffe58a', '#ff5f8a'
WIN_WARM, WIN_COOL = '#ffd27a', '#7fe9ff'
BRAND = 'TWINKIESGHOST'


CONFIG = json.load(open(os.path.join(HERE, 'style.json'), encoding='utf-8'))   # Leon's picks (see _picked)


def thumb_variant(kind='episode'):
    """Leon 10-22: sunset for every episode series, neon for highlight compilations (style.json)."""
    return CONFIG['thumb_variant_by_kind'].get(kind, CONFIG['thumb_variant_default'])


class TextOverflow(Exception):
    pass


def hexrgb(h, a=255):
    h = h.lstrip('#')
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def mix(a, b, t):
    a, b = hexrgb(a), hexrgb(b)
    return '#%02x%02x%02x' % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ── text: normalise, measure, wrap, fit ─────────────────────────────────────────────────────────────
SUBS = {'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-', '…': '...', '@': '', '_': ' '}


def clean(s):
    """Uppercase and keep only characters the pixel font has. Returns (text, dropped_chars)."""
    s = ''.join(SUBS.get(c, c) for c in str(s)).upper()
    keep, drop = [], []
    for c in s:
        (keep if c in G else drop).append(c)
    return re.sub(r'\s+', ' ', ''.join(keep)).strip(), ''.join(sorted(set(drop) - {' '}))


def glyph_w(c):
    return len(G[c][0])


def text_w(s, k):
    """Exact width in px of s at cell size k (same rule as obs/title.js bigW: glyph + 2 cells gap)."""
    return max(0, sum((glyph_w(c) + 2) * k for c in s) - 2 * k) if s else 0


ROWS_UP, ROWS_CAP, ROWS_DN = 1, 11, 2          # line box: 1 row above caps ('$'), 11 caps, 2 below (',')


def line_h(k):
    return (ROWS_UP + ROWS_CAP + ROWS_DN) * k


def wrap(s, k, maxw, maxlines):
    words, lines, cur = s.split(' '), [], ''
    for w in words:
        if text_w(w, k) > maxw:
            return None
        t = (cur + ' ' + w).strip()
        if text_w(t, k) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        return None
    return lines


def balance(lines, k, maxw):
    """Re-wrap the same number of lines with the narrowest widest-line (no 1-word orphan last line)."""
    n = len(lines)
    if n < 2:
        return lines
    words = ' '.join(lines).split(' ')
    best, best_w = lines, max(text_w(l, k) for l in lines)
    def rec(i, parts):
        nonlocal best, best_w
        if len(parts) == n - 1:
            parts = parts + [' '.join(words[i:])]
            if not parts[-1]:
                return
            w = max(text_w(p, k) for p in parts)
            if w <= maxw and w < best_w:
                best, best_w = parts, w
            return
        for j in range(i + 1, len(words) - (n - 2 - len(parts))):
            rec(j, parts + [' '.join(words[i:j])])
    if len(words) <= 14:
        rec(0, [])
    return best


def fit(s, maxw, maxh, kmax, kmin, maxlines):
    """Largest integer cell size k at which s wraps into <= maxlines lines inside maxw x maxh. Measured, not estimated."""
    for k in range(kmax, kmin - 1, -1):
        lines = wrap(s, k, maxw, maxlines)
        if lines and len(lines) * line_h(k) + (len(lines) - 1) * k <= maxh:
            return k, balance(lines, k, maxw)
    raise TextOverflow(f'"{s}" does not fit {maxw}x{maxh} in {maxlines} lines even at cell {kmin}px')


def text_mask(s, k):
    """L-mode mask of one line, height line_h(k), caps start at row ROWS_UP."""
    im = Image.new('L', (max(1, text_w(s, k)), line_h(k)), 0)
    d = ImageDraw.Draw(im)
    x = 0
    for c in s:
        rows = G[c]
        oy = ROWS_UP + (-1 if c == '$' else 0)
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch == '#':
                    d.rectangle([x + i * k, (oy + j) * k, x + (i + 1) * k - 1, (oy + j + 1) * k - 1], fill=255)
        x += (len(rows[0]) + 2) * k
    return im


def dilate(mask, r):
    """Square dilation by r px: a pixel-art outline (no rounded corners)."""
    out = mask
    while r > 0:
        step = min(r, 12)
        out = out.filter(ImageFilter.MaxFilter(2 * step + 1))
        r -= step
    return out


def banded(mask, k, bands, y0=0, period=None):
    """Fill a mask with horizontal colour bands per pixel row of the font (the camp's banded sunset).
    y0 = top of the first line box, period = line pitch, so every line gets the full gradient."""
    h = mask.size[1]
    period = period or line_h(k)
    fill = Image.new('RGBA', mask.size)
    d = ImageDraw.Draw(fill)
    for r in range(h // k + 1):
        cap_row = ((r * k - y0) % period) // k - ROWS_UP
        t = min(max(cap_row / (ROWS_CAP - 1), 0), 1)
        d.rectangle([0, r * k, mask.size[0], (r + 1) * k - 1], fill=hexrgb(bands(t)))
    out = Image.new('RGBA', mask.size, (0, 0, 0, 0))
    out.paste(fill, (0, 0), mask)
    return out


def sunset_band(t):           # yellow top -> orange -> pink bottom, stepped like the sun in gta.js
    steps = [SUN_TOP, SUN_TOP, '#ffd27a', '#ffb36b', '#ffb36b', ORANGE, '#ff8a6a', '#ff7a78', SUN_BOT, SUN_BOT, SUN_BOT]
    return steps[min(int(t * (len(steps) - 1) + .5), len(steps) - 1)]


def neon_band(t):
    return '#fff3fd' if t < 0.6 else '#ffd0f4'


def title_block(lines, k, style='sunset', align='left', glow=1.0):
    """RGBA block: glow + ink outline + 1-cell drop shadow + banded fill. Returns (image, pad) where pad is the
    margin around the text box (the text box itself is exactly the measured lines)."""
    lh, gap = line_h(k), k
    W = max(text_w(l, k) for l in lines)
    H = len(lines) * lh + (len(lines) - 1) * gap
    pad = 14 * k
    mask = Image.new('L', (W + 2 * pad, H + 2 * pad), 0)
    for i, l in enumerate(lines):
        lm = text_mask(l, k)
        x = pad + (0 if align == 'left' else (W - lm.size[0]) // 2 // k * k if align == 'center' else W - lm.size[0])
        mask.paste(lm, (x, pad + i * (lh + gap)))
    # outline/glow work happens on the coarse cell grid (1 px per font pixel) and is scaled back up: same pixels,
    # ~k*k times less work (a full-res MaxFilter at k=12 cost ~10 s per thumbnail)
    cells = mask.resize((mask.width // k, mask.height // k), Image.NEAREST)
    gdil = lambda n: dilate(cells, n).resize(mask.size, Image.NEAREST)
    gglow = lambda n, blur: dilate(cells, n).filter(ImageFilter.GaussianBlur(blur)).resize(mask.size, Image.BILINEAR)
    canvas = Image.new('RGBA', mask.size, (0, 0, 0, 0))
    glow_col = PINK if style != 'neon-cyan' else CYAN
    if style.startswith('neon'):
        glow_m = gglow(1, 4)                                 # soft haze, not a slab: legibility comes from the rim
        g = Image.new('RGBA', mask.size, hexrgb(glow_col)); g.putalpha(glow_m.point(lambda v: min(255, int(v * 1.3 * glow))))
        canvas.alpha_composite(g)
        e = Image.new('RGBA', mask.size, hexrgb(INK)); e.putalpha(gdil(1))   # 1-cell dark rim
        canvas.alpha_composite(e)
        tube = Image.new('RGBA', mask.size, hexrgb(glow_col)); tube.putalpha(dilate(mask, max(1, k // 3)))
        canvas.alpha_composite(tube)                         # thin hot tube edge hugging the glass
        canvas.alpha_composite(banded(mask, k, neon_band, pad, lh + gap))
    else:
        glow_m = gglow(2, 4)
        g = Image.new('RGBA', mask.size, hexrgb(PINK)); g.putalpha(glow_m.point(lambda v: int(v * 0.75 * glow)))
        canvas.alpha_composite(g)
        edge = gdil(1)
        sh = Image.new('RGBA', mask.size, (0, 0, 0, 0))
        s = Image.new('RGBA', mask.size, hexrgb('#5a2468')); s.putalpha(edge)
        sh.alpha_composite(s, (k, k))                       # 1-cell drop shadow in sunset purple
        canvas.alpha_composite(sh)
        e = Image.new('RGBA', mask.size, hexrgb(INK)); e.putalpha(edge)
        canvas.alpha_composite(e)
        canvas.alpha_composite(banded(mask, k, sunset_band, pad, lh + gap))
    return canvas, pad


def small_text(s, k, fill, ink=INK):
    s, _ = clean(s)
    m = text_mask(s, k)
    pad = k
    big = Image.new('L', (m.size[0] + 2 * pad, m.size[1] + 2 * pad), 0)
    big.paste(m, (pad, pad))
    out = Image.new('RGBA', big.size, (0, 0, 0, 0))
    e = Image.new('RGBA', big.size, hexrgb(ink)); e.putalpha(dilate(big, k))
    out.alpha_composite(e)
    f = Image.new('RGBA', big.size, hexrgb(fill)); f.putalpha(big)
    out.alpha_composite(f)
    return out, pad


def plate(text, k, fg, bg, border):
    """A pixel badge: text on a solid plate with a 1-cell border (episode number, series tag, brand)."""
    s, _ = clean(text)
    m = text_mask(s, k)
    px, py = 3 * k, 1 * k
    W, H = m.size[0] + 2 * px, m.size[1] - k + 2 * py      # trim the empty descender row a little
    im = Image.new('RGBA', (W + 2 * k, H + 2 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([k, k, W + k - 1, H + k - 1], fill=hexrgb(border))           # border, chamfered corners below
    d.rectangle([2 * k, 2 * k, W - 1, H - 1], fill=hexrgb(bg))
    for (x, y) in [(k, k), (W, k), (k, H), (W, H)]:
        d.rectangle([x, y, x + k - 1, y + k - 1], fill=(0, 0, 0, 0))
    f = Image.new('RGBA', m.size, hexrgb(fg)); f.putalpha(m)
    im.alpha_composite(f, (k + px, k + py - k // 2))
    glow = im.split()[3].filter(ImageFilter.GaussianBlur(2 * k))
    out = Image.new('RGBA', im.size, (0, 0, 0, 0))
    gl = Image.new('RGBA', im.size, hexrgb(bg if bg != INK else border)); gl.putalpha(glow.point(lambda v: int(v * .35)))
    out.alpha_composite(gl); out.alpha_composite(im)
    return out


# ── frame grade ────────────────────────────────────────────────────────────────────────────────────
def trim_bars(im, thresh=14, max_frac=.2):
    """Cut cinematic letterbox/pillarbox bars (rows/cols that are near-black end to end)."""
    g = ImageOps.grayscale(im).resize((160, 90))
    px = g.load()
    row = lambda y: max(px[x, y] for x in range(160)) < thresh
    col = lambda x: max(px[x, y] for y in range(90)) < thresh
    t = 0
    while t < 90 * max_frac and row(t): t += 1
    b = 89
    while b > 90 * (1 - max_frac) and row(b): b -= 1
    l = 0
    while l < 160 * max_frac and col(l): l += 1
    r = 159
    while r > 160 * (1 - max_frac) and col(r): r -= 1
    if (t, b, l, r) == (0, 89, 0, 159):
        return im
    sx, sy = im.width / 160, im.height / 90
    return im.crop((int((l + (l > 0)) * sx), int((t + (t > 0)) * sy), int((r + 1 - (r < 159)) * sx), int((b + 1 - (b < 89)) * sy)))


def cover_crop(im, W, H, focus=(0.5, 0.5)):
    im = trim_bars(im.convert('RGB'))
    s = max(W / im.width, H / im.height)
    im = im.resize((max(W, round(im.width * s)), max(H, round(im.height * s))), Image.LANCZOS)
    x = round((im.width - W) * focus[0]); y = round((im.height - H) * focus[1])
    return im.crop((x, y, x + W, y + H))


def vice_grade(im, strength=1.0):
    """Punchier frame with a Vice dusk cast: contrast + saturation up, shadows toward night purple, highlights warm."""
    im = ImageEnhance.Contrast(im).enhance(1 + .15 * strength)
    im = ImageEnhance.Color(im).enhance(1 + .25 * strength)
    g = ImageOps.grayscale(im)
    duo = ImageOps.colorize(g, black='#1b0f3a', mid='#9d3472', white='#ffe9c8')
    return Image.blend(im, duo, .22 * strength)


def vignette(W, H, col=INK, edge=.85, power=2.2):
    m = Image.radial_gradient('L').resize((W, H))
    m = m.point(lambda v: int(min(255, (v / 255) ** power * 255 * edge)))
    v = Image.new('RGBA', (W, H), hexrgb(col)); v.putalpha(m)
    return v


def grad(W, H, top, bot, a0, a1):
    """Vertical gradient layer from colour top@a0 to bot@a1."""
    im = Image.new('RGBA', (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / max(1, H - 1)
        c = hexrgb(mix(top, bot, t))
        d.line([(0, y), (W, y)], fill=(c[0], c[1], c[2], round(a0 + (a1 - a0) * t)))
    return im


# ── pixel-art decor (drawn on a coarse grid, upscaled NEAREST like the camp) ─────────────────────────
def pixel_layer(W, H, cell):
    return Image.new('RGBA', (W // cell + 1, H // cell + 1), (0, 0, 0, 0))


def up(layer, W, H, cell):
    return layer.resize((layer.width * cell, layer.height * cell), Image.NEAREST).crop((0, 0, W, H))


def skyline(layer, base, seed, hmin=8, hmax=30, neon=True):
    """Night skyline silhouette standing on row `base`, lit windows + neon roof strips (gta.js recipe)."""
    R = random.Random(seed)
    d = ImageDraw.Draw(layer)
    x = -2
    while x < layer.width:
        w = R.randint(5, 13); h = R.randint(hmin, hmax); top = base - h
        tower = R.random() < .18
        if tower:
            h = int(h * 1.35); top = base - h
        d.rectangle([x, top, x + w - 1, base], fill=hexrgb('#21133a'))
        d.rectangle([x + w - 1, top, x + w - 1, base], fill=hexrgb('#3d2556'))
        for wy in range(top + 2, base - 1, 2):
            for wx in range(x + 1, x + w - 2, 2):
                r = R.random()
                if r < .26:
                    d.point((wx, wy), fill=hexrgb(WIN_COOL if r < .03 else WIN_WARM))
        if tower:
            d.rectangle([x + w // 2, top - 5, x + w // 2, top - 1], fill=hexrgb('#c9a7ff'))
        if neon and R.random() < .3:
            d.rectangle([x + 1, top + 1, x + w - 2, top + 1], fill=hexrgb([PINK, CYAN, ORANGE][R.randrange(3)]))
        x += w + R.randint(0, 2)


def sunset_scene(W, H, cell, seed=7, sun=True, horizon=.62):
    """The camp's dusk: banded sky, striped sun, sea with sun glints, skyline. Opaque RGBA."""
    L = pixel_layer(W, H, cell)
    d = ImageDraw.Draw(L)
    hz = int(L.height * horizon)
    for y in range(hz):
        i = min(len(SKY) - 1, int(y / hz * len(SKY)))
        d.line([(0, y), (L.width, y)], fill=hexrgb(SKY[i]))
    R = random.Random(seed)
    for _ in range(L.width * hz // 90):
        x, y = R.randrange(L.width), R.randrange(max(1, int(hz * .4)))
        d.point((x, y), fill=hexrgb('#fff1d8' if R.random() < .2 else '#b8a8e0'))
    if sun:
        cx, r = int(L.width * .62), max(6, int(min(L.width, hz) * .26))
        cy = hz - r // 3
        for y in range(cy - r, min(hz, cy + r)):
            dy = y - cy
            if dy * dy > r * r:
                continue
            band = (y - (cy - r)) / (2 * r)
            if dy > 0 and (y // 2) % (2 + int((1 - band) * 3)) == 0:
                continue                                 # the retro sun's cut stripes
            half = int((r * r - dy * dy) ** .5)
            d.line([(cx - half, y), (cx + half, y)], fill=hexrgb(mix(SUN_TOP, SUN_BOT, band)))
    skyline(L, hz, seed + 1, hmin=max(4, hz // 8), hmax=max(8, hz // 3))
    for y in range(hz + 1, L.height):
        t = (y - hz) / max(1, L.height - hz)
        d.line([(0, y), (L.width, y)], fill=hexrgb(mix('#3c1a5e', '#120a26', min(1, t * 1.4))))
        if y % 3 == 0:                                      # slow swell lines across the sea
            for _ in range(max(1, L.width // 14)):
                x = R.randrange(L.width)
                d.line([(x, y), (x + R.randint(2, 6), y)], fill=hexrgb(mix('#56216a', '#29144d', t)))
        if sun and t < .45 and (y - hz) % 2 == 0:           # sun glitter: short broken bars that narrow and fade
            g = int(L.width * .62)
            w = max(1, int(L.width * .16 * (1 - t / .45)))
            for _ in range(2):
                x = g + R.randint(-w, w)
                d.line([(x - R.randint(1, 3), y), (x + R.randint(1, 3), y)], fill=hexrgb(mix('#ffd27a', '#ff7fa0', t / .45)))
    return up(L, W, H, cell)


def deco_strip(W, H, cell, seed):
    """Transparent bottom strip: skyline silhouette, for laying over a frame."""
    L = pixel_layer(W, H, cell)
    skyline(L, L.height, seed, hmin=max(3, L.height // 6), hmax=max(6, L.height // 2))
    return up(L, W, H, cell)


def neon_frame(W, H, inset, k, col=CYAN, col2=PINK):
    """Double neon tube border around the picture (pixel corners)."""
    m = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(m)
    d.rectangle([inset, inset, W - inset - 1, H - inset - 1], outline=255, width=k)
    m2 = Image.new('L', (W, H), 0)
    d2 = ImageDraw.Draw(m2)
    i2 = inset + 3 * k
    d2.rectangle([i2, i2, W - i2 - 1, H - i2 - 1], outline=255, width=max(1, k // 2))
    out = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    for mm, c in ((m, col), (m2, col2)):
        g = Image.new('RGBA', (W, H), hexrgb(c)); g.putalpha(mm.filter(ImageFilter.GaussianBlur(3 * k)).point(lambda v: min(255, v * 3)))
        out.alpha_composite(g)
        t = Image.new('RGBA', (W, H), hexrgb(mix(c, '#ffffff', .55))); t.putalpha(mm)
        out.alpha_composite(t)
    return out


# ── templates ──────────────────────────────────────────────────────────────────────────────────────
def _load(frame):
    if isinstance(frame, Image.Image):
        return frame
    if not frame or not os.path.exists(frame):
        raise FileNotFoundError(f'frame not found: {frame}')
    return Image.open(frame)


def _prep_title(title, maxw, maxh, kmax, kmin, maxlines):
    t, dropped = clean(title)
    if not t:
        raise ValueError('title is empty after cleaning')
    k, lines = fit(t, maxw, maxh, kmax, kmin, maxlines)
    return k, lines, dropped


def episode_thumb(frame, title, ep=None, series='', out=None, variant='sunset', seed=None, focus=(0.5, 0.5), steps=None):
    """1280x720 YouTube episode thumbnail. Returns {'out','k','lines','dropped'}."""
    W, H = 1280, 720
    seed = seed if seed is not None else (sum(map(ord, str(title))) + int(ep or 0))
    src = _load(frame)
    info = {}
    if variant == 'split':
        PW = 560                                                 # left pixel-art panel
        img = Image.new('RGBA', (W, H))
        pic = vice_grade(cover_crop(src, W - PW + 80, H, focus), .8).convert('RGBA')
        img.paste(pic, (PW - 80, 0))
        panel = sunset_scene(PW, H, 6, seed=seed, horizon=.66)
        # stepped diagonal edge: the panel bites into the picture in 6 px stairs
        m = Image.new('L', (PW, H), 255)
        d = ImageDraw.Draw(m)
        for y in range(0, H, 24):
            d.rectangle([PW - 80 + (y // 24) * 80 // 30, y, PW, y + 23], fill=0)
        img.paste(panel, (0, 0), m)
        edge = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        de = ImageDraw.Draw(edge)
        for y in range(0, H, 24):
            x = PW - 80 + (y // 24) * 80 // 30
            de.rectangle([x, y, x + 5, y + 23], fill=hexrgb(PINK))
        img.alpha_composite(edge.filter(ImageFilter.GaussianBlur(6)))
        img.alpha_composite(edge)
        y = 34
        if ep is not None:
            p = plate(f'EP {ep}', 5, INK, CYAN, '#ffffff')
            img.alpha_composite(p, (30, y)); y += p.height + 10
        if series:
            s = plate(series, 3, '#ffffff', INK, PINK)
            if s.width <= PW - 110:
                img.alpha_composite(s, (30, y)); y += s.height + 10
        k, lines, dropped = _prep_title(title, PW - 120, 440 - y, 9, 4, 4)
        blk, pad = title_block(lines, k, 'sunset')
        img.alpha_composite(blk, (40 - pad, y + 14 - pad))
        b, _ = small_text(BRAND, 3, '#ffffff')
        img.alpha_composite(b, (W - b.width - 24, 22))
    elif variant == 'neon':
        img = vice_grade(cover_crop(src, W, H, focus), 1.0).convert('RGBA')
        img = Image.blend(img, Image.new('RGBA', (W, H), hexrgb(INK)), .12)        # Leon 10-22: keep the game visible
        img.alpha_composite(vignette(W, H, edge=.5))
        img.alpha_composite(neon_frame(W, H, 14, 4))
        img.alpha_composite(grad(W, 220, '#120a26', '#120a26', 0, 160), (0, H - 220))
        k, lines, dropped = _prep_title(title, 760, 175, 7, 4, 2)
        blk, pad = title_block(lines, k, 'neon', align='center', glow=.5)
        img.alpha_composite(blk, ((W - blk.width) // 2, H - 50 - (blk.height - 2 * pad) - pad))
        if ep is not None:
            img.alpha_composite(plate(f'EP {ep}', 4, INK, PINK, '#ffffff'), (34, 34))
        if series:
            s = plate(series, 2, '#ffffff', INK, CYAN)
            if s.width <= W - 400:
                img.alpha_composite(s, (34, 34))
        b, _ = small_text(BRAND, 2, '#ffffff')
        img.alpha_composite(b, (W - b.width - 38, 36))
    else:  # sunset (default)
        snap = (lambda name: steps.append((name, img.convert('RGB').copy()))) if steps is not None else (lambda name: None)
        img = cover_crop(src, W, H, focus).convert('RGBA'); snap('crop')
        # Leon 10-22: "the text is a bit too much and loud, we need to know its the game": the frame is the hero.
        # Light grade, a short dusk floor, thin skyline, a 2-line title capped at cell 7 with a faint glow.
        img = vice_grade(img.convert('RGB'), .45).convert('RGBA')
        img.alpha_composite(vignette(W, H, edge=.4))
        img.alpha_composite(grad(W, 210, '#120a26', '#3c1a5e', 0, 175), (0, H - 210))   # dusk floor under the title only
        img.alpha_composite(deco_strip(W, 60, 4, seed), (0, H - 60)); snap('grade')
        k, lines, dropped = _prep_title(title, 880, 175, 7, 4, 2)
        blk, pad = title_block(lines, k, 'sunset', glow=.35)
        th = blk.height - 2 * pad
        img.alpha_composite(blk, (40 - pad, H - 40 - th - pad)); snap('title')
        b, _ = small_text(BRAND, 2, '#ffffff')
        bx = W - b.width - 24
        img.alpha_composite(b, (bx, 24))
        p = plate(f'EP {ep}', 4, INK, CYAN, '#ffffff') if ep is not None else None
        if p:
            img.alpha_composite(p, (26, 24))
        if series:
            s = plate(series, 2, '#ffffff', INK, PINK)
            x = 26 + (p.width + 12 if p else 0)
            if x + s.width <= bx - 24:                       # beside the badge when it clears the brand tag
                img.alpha_composite(s, (x, 24 + ((p.height - s.height) // 2 if p else 0)))
            elif 26 + s.width <= bx - 24 or p:               # else under the badge
                img.alpha_composite(s, (26, 24 + (p.height + 6 if p else 0)))
    info.update(k=k, lines=lines, dropped=dropped)
    return _save(img, out, info)


def short_cover(frame, title, out=None, variant='sunset', seed=None, focus=(0.5, 0.5), series=''):
    """1080x1920 Shorts cover / first frame. Title sits in the top band, clear of the Shorts UI (bottom 25%, right rail)."""
    W, H = 1080, 1920
    seed = seed if seed is not None else sum(map(ord, str(title)))
    src = _load(frame)
    img = vice_grade(cover_crop(src, W, H, focus), 1.0).convert('RGBA')
    img.alpha_composite(vignette(W, H, edge=.75))
    if variant == 'neon':
        img = Image.blend(img, Image.new('RGBA', (W, H), hexrgb(INK)), .25)
        img.alpha_composite(neon_frame(W, H, 24, 8))
        k, lines, dropped = _prep_title(title, W - 200, 560, 14, 6, 4)
        blk, pad = title_block(lines, k, 'neon', align='center')
        img.alpha_composite(blk, ((W - blk.width) // 2, 230 - pad))
    else:
        img.alpha_composite(grad(W, 820, '#120a26', '#120a26', 235, 0), (0, 0))
        k, lines, dropped = _prep_title(title, W - 160, 560, 14, 6, 4)
        blk, pad = title_block(lines, k, 'sunset', align='center')
        img.alpha_composite(blk, ((W - blk.width) // 2, 260 - pad))
        img.alpha_composite(grad(W, 500, '#120a26', '#56216a', 0, 200), (0, H - 500))
        img.alpha_composite(deco_strip(W, 300, 8, seed), (0, H - 300))

    if series:
        s = plate(series, 4, '#ffffff', INK, PINK)
        if s.width <= W - 120:
            img.alpha_composite(s, ((W - s.width) // 2, 120 if variant != 'neon' else 110))
    return _save(img, out, dict(k=k, lines=lines, dropped=dropped))


def _save(img, out, info):
    img = img.convert('RGB')
    if out:
        tmp = out + '.part.png'
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        img.save(tmp, 'PNG', compress_level=6)
        if os.path.getsize(tmp) > 2 * 1024 * 1024:            # YouTube custom thumbnail cap is 2 MB
            os.remove(tmp)
            tmp = os.path.splitext(out)[0] + '.part.jpg'
            img.save(tmp, 'JPEG', quality=92)
            out = os.path.splitext(out)[0] + '.jpg'
        os.replace(tmp, out)
        info['out'] = out
    info['image'] = img
    return info


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────
def _run(job):
    kind = job.get('kind', 'thumb')
    if kind == 'thumb':
        r = episode_thumb(job['frame'], job['title'], job.get('ep'), job.get('series', ''), job['out'],
                          job.get('variant') or thumb_variant(job.get('content', 'episode')))
    elif kind == 'cover':
        r = short_cover(job['frame'], job['title'], job['out'], job.get('variant') or CONFIG['cover_variant'], series=job.get('series', ''))
    else:
        raise ValueError(f'unknown kind {kind}')
    r.pop('image', None)
    return r


def main(argv):
    import argparse
    if argv and argv[0] == 'batch':
        jobs = json.load(open(argv[1], encoding='utf-8'))
        res, bad = [], 0
        for j in jobs:
            try:
                res.append(dict(ok=True, **_run(j)))
            except (TextOverflow, ValueError, FileNotFoundError) as e:
                bad += 1
                res.append(dict(ok=False, out=j.get('out'), error=str(e)))
        print(json.dumps(res, indent=1))
        return 2 if bad else 0
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['thumb', 'cover'])
    ap.add_argument('--frame', required=True); ap.add_argument('--title', required=True)
    ap.add_argument('--ep'); ap.add_argument('--series', default='')
    ap.add_argument('--variant', default=None)
    ap.add_argument('--content', choices=['episode', 'highlights'], default='episode'); ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    try:
        r = _run(vars(a))
    except TextOverflow as e:
        print('OVERFLOW:', e, file=sys.stderr); return 2
    except (ValueError, FileNotFoundError) as e:
        print('BAD INPUT:', e, file=sys.stderr); return 3
    print(json.dumps(r))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
