"""Camp-style intro sting + outro for the polished episodes (the owner's pick 12 Nov: look "A Sky title", music "M2 pixel camp").

Drop-in for polished.brand.stings: the same intro()/outro() signatures, the same 4.6 s / 20.0 s CONTRACT
(INTRO_D/OUTRO_D in plan_cut + review), 1920x1080, 60 fps, AAC 48 kHz.

    python -m polished.brand.camp intro "<title>" --game "GTA 6" --brand <name> --login <twitch> --out <dir>
    python -m polished.brand.camp outro --game "GTA 6" --brand <name> --login <twitch> --out <dir>

Look: the channel's own autumn camp (the OBS camp engine, recorded frame-exact at its native 480x270 into
camp_assets/*_base.mp4, lossless) with titles in the kit's bold pixel capitals (camp_assets/pixel_caps.json,
from the OBS kit's title.js). Everything is drawn on the 480x270 grid and scaled exactly 4x, so every pixel is
a solid 4x4 block like the camp. Music: original 8-bit, composed in code (no samples, no AI), mastered to
-26 LUFS to sit level with the episodes. The channel name and Twitch login come from the caller (identity
secret), never from this file: the repo is public. Every drawn string is checked against the retired-name
list (refused before a single frame is written) and logged to <out>/_drawn.json.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "camp_assets")
W, H, S, FPS, SR = 480, 270, 4, 60, 48000
INTRO_D, OUTRO_D = 4.6, 20.0
CREAM, SHADOW, MINT, SOFT, GREEN = (246, 231, 200), (36, 24, 15), (127, 255, 212), (200, 225, 220), (140, 252, 110)
FONT = json.load(open(os.path.join(ASSETS, "pixel_caps.json"), encoding="utf-8"))
# YouTube end-screen element areas at 1920x1080 (video 620x349 at (120,400), subscribe r148 at (960,574)) / 4
BOX = (30, 100, 185, 187)
SUB = (240, 143, 37)
MAXW = 452
DRAWN = []


# ------------------------------------------------------------------ guard
def squash(x):
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def _retired(retired):
    if retired:
        return [n for n in (squash(x) for x in retired) if n]
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
        from clipbot import common
        return [n for n in (squash(x) for x in common.IDENT.get("legacy", [])) if n]
    except SystemExit:
        return []


class Guard:
    def __init__(self, retired):
        self.retired = _retired(retired)
        if not self.retired:
            raise SystemExit("no retired-name list in the channel identity: refusing to render unchecked")

    def __call__(self, text):
        if any(n in squash(text) for n in self.retired):
            raise SystemExit("refusing to draw a retired channel name into a sting")
        DRAWN.append(str(text))
        return text


# ------------------------------------------------------------------ pixel text
def clean(text):
    """Upper case, only glyphs the font has (common punctuation mapped), single spaces."""
    t = str(text).upper().replace("’", "'").replace("‘", "'").replace('"', "'").replace("—", "-") \
        .replace("–", "-").replace("…", "...").replace("|", "-").replace("_", " ")
    t = "".join(c for c in t if c in FONT)
    return re.sub(r"\s+", " ", t).strip()


def width(t, k=1, gap=1):
    gl = [FONT[c] for c in t]
    return sum(len(g[0]) * k for g in gl) + gap * k * max(0, len(gl) - 1)


def layer(t, k=1, gap=1, col=CREAM, shadow=SHADOW):
    """RGBA text layer on the art grid (shadow 1 font-px down, like the kit)."""
    w = max(1, width(t, k, gap))
    img = Image.new("RGBA", (w, 13 * k + k), (0, 0, 0, 0))
    px = img.load()
    for dy, c in (((k, shadow),) if shadow else ()) + ((0, col),):
        cx = 0
        for ch in t:
            g = FONT[ch]
            oy = 0 if len(g) > 11 else 1
            for j, row in enumerate(g):
                for i, b in enumerate(row):
                    if b == "#":
                        for u in range(k):
                            for v in range(k):
                                X, Y = cx + i * k + u, (j + oy) * k + v + dy
                                if 0 <= X < img.width and 0 <= Y < img.height:
                                    px[X, Y] = c + (255,)
            cx += len(g[0]) * k + gap * k
    return img


def fit(t, sizes=(3, 2, 1), maxw=MAXW):
    for k in sizes:
        if width(t, k) <= maxw:
            return [t], k
    words, lines = t.split(" "), [""]
    for wd in words:          # wrap at k=1, balanced-ish, max 3 lines
        trial = (lines[-1] + " " + wd).strip()
        if width(trial) <= maxw or not lines[-1]:
            lines[-1] = trial
        else:
            lines.append(wd)
    while len(lines) > 3:
        lines[-2] = lines[-2] + " " + lines.pop()
    return [ln if width(ln) <= maxw else ln[: max(1, len(ln) * maxw // max(1, width(ln)) - 1)] for ln in lines], 1


def put(img, lay, cx, y, alpha=1.0):
    if alpha <= 0:
        return
    lay = lay if alpha >= 1 else _alpha(lay, alpha)
    img.alpha_composite(lay, (int(cx - lay.width // 2), int(y)))


def _alpha(lay, a):
    l2 = lay.copy()
    l2.putalpha(l2.getchannel("A").point(lambda v: int(v * a)))
    return l2


def band(img, y0, y1, alpha=95):
    """See-through dark band (composited, not painted: ImageDraw on RGBA REPLACES pixels), dithered edges."""
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.rectangle([0, y0, W, y1], fill=(20, 12, 24, alpha))
    for x in range(0, W, 2):
        d.point((x + (y0 % 2), y0 - 1), fill=(20, 12, 24, alpha))
        d.point((x + (y1 % 2), y1 + 1), fill=(20, 12, 24, alpha))
    img.alpha_composite(ov)


# ------------------------------------------------------------------ plates + layout (owner 12 Nov: "the wording can be better placed")
PAD_X, PAD_Y = 8, 6


def plate(w, h, alpha=1.0):
    """A see-through pixel panel sized to its text: 2 px drop shadow, dark fill, 1 px dark edge, 1 px light rim,
    notched (pixel-rounded) corners. Reads as part of the pixel art, not a strip stamped across the frame."""
    im = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def a(v):
        return int(v * alpha)
    d.rectangle([3, 3, w + 1, h + 1], fill=(0, 0, 0, a(90)))                     # shadow
    d.rectangle([1, 0, w - 2, h - 1], fill=(14, 9, 22, a(196)))
    d.rectangle([0, 1, w - 1, h - 2], fill=(14, 9, 22, a(196)))
    for xy in ([2, 0, w - 3, 0], [2, h - 1, w - 3, h - 1], [0, 2, 0, h - 3], [w - 1, 2, w - 1, h - 3]):
        d.line(xy, fill=(8, 5, 12, a(230)))                                          # dark edge
    d.line([2, 1, w - 3, 1], fill=(246, 231, 200, a(70)))                            # light rim, top + left
    d.line([1, 2, 1, h - 3], fill=(246, 231, 200, a(50)))
    return im


def wrap(t, k, maxw, maxlines=3):
    """Balanced line breaks: the fewest lines, then the split whose longest line is shortest (no one-word widows)."""
    words = t.split(" ")
    best = [None]

    def key(ls):
        return (len(ls), max(width(l, k) for l in ls))

    def rec(i, lines):
        if i == len(words):
            if max(width(l, k) for l in lines) <= maxw and (best[0] is None or key(lines) < key(best[0])):
                best[0] = list(lines)
            return
        if len(lines) < maxlines:
            rec(i + 1, lines + [words[i]])
        if lines:
            rec(i + 1, lines[:-1] + [lines[-1] + " " + words[i]])
    if len(words) <= 14:
        rec(0, [])
    return best[0]


def game_lines(t, maxw):
    """Game title: as big as it fits. 3x on one line, else 2x on one line, else 2x on two balanced lines, else 1x."""
    for k, n in ((3, 1), (2, 1), (2, 2), (1, 2), (1, 3)):
        ls = wrap(t, k, maxw, n)
        if ls:
            return ls, k
    return fit(t, (1,), maxw)


def stack(rows):
    """rows = [(layer, gap_after)] -> (w, h, [(layer, y)]) of a centred text block."""
    y, out = 0, []
    for lay, gap in rows:
        out.append((lay, y))
        y += lay.height + gap
    return max(l.width for l, _ in rows), y - rows[-1][1], out


def divider(w):
    im = Image.new("RGBA", (w, 1), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 2):
        d.point((x, 0), fill=MINT + (150,))
    return im


def ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def stepfade(a):
    return round(max(0.0, min(1.0, a)) * 8) / 8          # 8 levels: a pixel fade, not a smooth one


# ------------------------------------------------------------------ frames + encode
def base_frames(name):
    """Decode a camp recording; a 240x135 (2x zoom) shot is scaled up on the pixel grid to 480x270."""
    path = os.path.join(ASSETS, name)
    wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip().split(",")
    w, h = int(wh[0]), int(wh[1])
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE)
    n = w * h * 3
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        im = Image.frombytes("RGB", (w, h), b)
        yield im if (w, h) == (W, H) else im.resize((W, H), Image.NEAREST)
    p.wait()


# ------------------------------------------------------------------ which camp, which variant
VARIANTS = json.load(open(os.path.join(ASSETS, "variants.json"), encoding="utf-8"))
VERSION = json.load(open(os.path.join(ASSETS, "manifest.json"), encoding="utf-8"))["version"]   # in cache keys: new art = new outro


def need(name):
    """The media is fetched + sha256-checked by camp_fetch before deploy; refuse clearly if a render host lacks it."""
    if not os.path.exists(os.path.join(ASSETS, name)):
        raise SystemExit("camp sting media missing: %s (run python -m polished.brand.camp_fetch)" % name)
    return name
LAYOUT = json.load(open(os.path.join(ASSETS, "outro_layout.json"), encoding="utf-8"))   # calmest CTA corner per camp (recorder/calm.py)


def camp_for(game):
    g = squash(game)
    if g.startswith("gta") or "grandtheftauto" in g:
        return "gta"
    if "cyberpunk" in g:
        return "cp"
    return "autumn"


def pick_variant(game, title, index=None):
    """The episode's intro shot: its game's camp, rotated by the episode index (consecutive episodes never repeat;
    the same index always gives the same shot). No index: a stable hash of the title (same title = same shot)."""
    vs = VARIANTS.get(camp_for(game)) or VARIANTS["autumn"]
    if index is None:
        import hashlib
        index = int(hashlib.sha1(squash(title).encode()).hexdigest()[:8], 16)
    return vs[int(index) % len(vs)]


def encode(frames, nframes, audio, out):
    tmp = out + ".part.mp4"
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
                            "-r", str(FPS), "-i", "-", "-i", audio,
                            "-vf", "scale=%d:%d:flags=neighbor" % (W * S, H * S),
                            "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(FPS),
                            "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-t", "%.3f" % (nframes / FPS),
                            "-movflags", "+faststart", tmp], stdin=subprocess.PIPE)
    n = 0
    for fr in frames:
        enc.stdin.write(fr.convert("RGB").tobytes())
        n += 1
        if n >= nframes:
            break
    enc.stdin.close()
    if enc.wait() != 0 or n != nframes:
        raise RuntimeError("sting encode failed (%d/%d frames)" % (n, nframes))
    os.replace(tmp, out)
    return out


def _save_drawn(out):
    json.dump(sorted(set(DRAWN)), open(os.path.join(out, "_drawn.json"), "w", encoding="utf-8"), indent=0)


# ------------------------------------------------------------------ intro: "Sky title", golden-hour camp
def intro(title, game, brand, login, out, retired=None, index=None):
    """-> path of the 4.6 s intro: brand name, game and episode title over the game's camp (variant by index). No Part number."""
    g = Guard(retired)
    v = pick_variant(game, title, index)
    os.makedirs(out, exist_ok=True)
    for t in (title, game, brand, login):
        g(t)
    name_l = layer(g(clean(brand)), 1, 2, MINT)
    glines, gk = game_lines(g(clean(game)), MAXW)                     # 3x if it fits, else 2x, else 2x on two lines ...
    game_ls = [layer(ln, gk, 1) for ln in glines]
    tlines = wrap(g(clean(title)), 1, MAXW, 3) or fit(clean(title), (1,), MAXW)[0]
    title_ls = [layer(ln, 1, 1) for ln in tlines]
    y_name, y_game = 24, 42
    y_title = y_game + sum(l.height for l in game_ls) + 4
    n = int(round(INTRO_D * FPS))

    def frames():
        for i, fr in enumerate(base_frames(need(v["file"]))):
            t = i / FPS
            img = fr.convert("RGBA")
            put(img, name_l, W // 2, round(-40 + (y_name + 40) * ease((t - 0.1) / 0.45)))
            if t > 0.35:
                a, dy = ease((t - 0.35) / 0.35), round(6 * (1 - ease((t - 0.35) / 0.35)))
                y = y_game - dy
                for l in game_ls:
                    put(img, l, W // 2, y, a)
                    y += l.height
            if t > 0.9:
                a = ease((t - 0.9) / 0.3)
                for j, l in enumerate(title_ls):
                    put(img, l, W // 2, y_title + j * 14, a)
            f = stepfade(min(t / 0.5, (INTRO_D - t) / 0.45))
            yield Image.blend(Image.new("RGB", (W, H)), img.convert("RGB"), f) if f < 1 else img
    tag = re.sub(r"[^a-z0-9]", "", game.lower()) or "game"
    dst = encode(frames(), n, os.path.join(ASSETS, "m2_intro.flac"), os.path.join(out, "%s_intro_%s.mp4" % (tag, os.getpid())))
    _save_drawn(out)
    with open(dst + ".variant.json", "w", encoding="utf-8") as f:
        json.dump({"variant": v["id"], "index": index}, f)
    return dst


# ------------------------------------------------------------------ outro: night camp + end-screen areas
PER_GAME_OUTRO = True     # the owner's pick 12 Nov: each game's own camp behind the outro (GTA beach, Badlands, autumn night)


def outro(game, brand, login, out, retired=None, per_game=None):
    g = Guard(retired)
    own = {"gta": "outro_gta.mp4", "cp": "outro_cp.mp4"}.get(camp_for(game), "camp_outro_base.mp4")
    if not (PER_GAME_OUTRO if per_game is None else per_game):
        own = "camp_outro_base.mp4"
    base = need(own)
    os.makedirs(out, exist_ok=True)
    for t in (game, brand, login):
        g(t)
    head = layer(g("THANKS FOR WATCHING"), 2, 1)
    nxt = layer(g("NEXT EPISODE"), 1, 1, SOFT)
    sub = layer(g("SUBSCRIBE"), 1, 1, GREEN)
    catch = layer(g("CATCH THE STREAMS LIVE AT"), 1, 1, SOFT)
    tw = clean("TWITCH.TV/" + g(login))
    twl = layer(g(tw), 2 if width(tw, 2) <= 300 else 1, 1, MINT)
    # the game sits IN the header plate, one mint line under THANKS FOR WATCHING: the same size on every outro, and the
    # scenery right of the end-screen elements stays clear (12 Nov: a loose 3x "GTA 6" fought the lifeguard tower)
    glines = wrap(g(clean(game)), 1, 280, 2) or fit(clean(game), (1,), 280)[0]
    game_ls = [layer(ln, 1, 1, MINT) for ln in glines]
    hw, hh, hrows = stack([(head, 5)] + [(l, 2) for l in game_ls[:-1]] + [(game_ls[-1], 0)])
    hp = plate(hw + 2 * PAD_X, hh + 2 * PAD_Y)
    hx, hy = (W - hp.width) // 2, 8
    cw, chh, crows = stack([(catch, 3), (twl, 0)])
    ctp = plate(cw + 2 * PAD_X, chh + 2 * PAD_Y)
    side, cty = LAYOUT.get(camp_for(game), {}).get("cta", ["c", 222])
    ctx = {"l": 12, "r": W - 12 - ctp.width}.get(side, (W - ctp.width) // 2)
    cty = min(cty, H - 6 - ctp.height)
    nxt_p, sub_p = plate(nxt.width + 6, nxt.height + 2), plate(sub.width + 6, sub.height + 2)
    n = int(round(OUTRO_D * FPS))

    def frames():
        for i, fr in enumerate(base_frames(need(base))):
            t = i / FPS
            img = fr.convert("RGBA")
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            x0, y0, x1, y1 = BOX
            pulse = 0.6 + 0.4 * math.sin(t * 2.2)
            d.rectangle([x0, y0, x1, y1], fill=(10, 8, 20, 150), outline=(int(127 * pulse + 60), 255, int(212 * pulse + 40), 255))
            cx, cy, r = SUB
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(10, 8, 20, 150), outline=(83, 252, 24, 255))
            img.alpha_composite(ov)
            img.alpha_composite(nxt_p, ((x0 + x1) // 2 - nxt_p.width // 2, y1 + 3))
            put(img, nxt, (x0 + x1) // 2, y1 + 4)
            img.alpha_composite(sub_p, (cx - sub_p.width // 2, cy + r + 3))
            put(img, sub, cx, cy + r + 4)
            hy_t = round(-50 + (hy + 50) * ease((t - 0.1) / 0.45))
            img.alpha_composite(hp, (hx, hy_t))
            for lay, y in hrows:
                put(img, lay, W // 2, hy_t + PAD_Y + y)
            img.alpha_composite(ctp, (ctx, cty))
            for lay, y in crows:
                put(img, lay, ctx + ctp.width // 2, cty + PAD_Y + y)
            f = stepfade(t / 0.6)
            yield Image.blend(Image.new("RGB", (W, H)), img.convert("RGB"), f) if f < 1 else img
    tag = re.sub(r"[^a-z0-9]", "", game.lower()) or "game"
    dst = encode(frames(), n, os.path.join(ASSETS, "m2_outro.flac"), os.path.join(out, "%s_outro.mp4" % tag))
    _save_drawn(out)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["intro", "outro"])
    ap.add_argument("title", nargs="?", default="")
    ap.add_argument("--game", required=True)
    ap.add_argument("--brand", required=True)
    ap.add_argument("--login", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(intro(a.title, a.game, a.brand, a.login, a.out) if a.what == "intro" else outro(a.game, a.brand, a.login, a.out))


if __name__ == "__main__":
    main()
