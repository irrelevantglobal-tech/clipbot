"""Intro sting + Twitch-first outro for the polished episodes (any game), on any machine.

The same sting code as the published WD1/WD2/Legion series (sting_core.py, numpy-synthesised audio,
YouTube end-screen plate geometry, intro 4.6 s / outro 20.0 s = a CONTRACT with render + chapters).
The game's mark is our own pixel lettering (clipbot/style/fonts/PixelCaps.ttf), never the publisher's:
GTA 6 rule, no Rockstar logos, fonts or trademarks. Channel name + Twitch login are arguments (from
the channel identity), never written here: the repo is public.

    python -m polished.brand.stings intro "<title>" --game "GTA 6" --brand <name> --login <twitch> --out <dir>
    python -m polished.brand.stings outro --game "GTA 6" --brand <name> --login <twitch> --out <dir>
    python -m polished.brand.stings frames "<title>" ...      # one intro + one outro frame, no encode
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PIXEL_TTF = os.path.join(os.path.dirname(os.path.dirname(HERE)), "clipbot", "style", "fonts", "PixelCaps.ttf")
sys.path.insert(0, HERE)
import sting_core as ws  # noqa: E402


def wordmark_png(text, path, height=400):
    """White pixel-capital wordmark on transparent, roughly square like the WD2 mark.
    One or two lines, rendered big then scaled with NEAREST so the pixel edges stay crisp."""
    words = text.upper().split()
    lines = [text.upper()] if len(text) <= 8 or len(words) == 1 else [" ".join(words[:-1]), words[-1]]
    fnt = ImageFont.truetype(PIXEL_TTF, 160)
    widths = [fnt.getlength(l) for l in lines]
    lh = 190
    img = Image.new("RGBA", (int(max(widths)) + 40, lh * len(lines) + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"   # no antialias: the TTF is built from pixel cells, AA leaves row seams
    for i, l in enumerate(lines):
        d.text(((img.width - widths[i]) / 2, 20 + i * lh), l, font=fnt, fill=(255, 255, 255, 255))
    img = img.crop(img.getbbox())
    side = max(img.width, img.height)   # sting_core scales by HEIGHT: pad a wide mark to a square
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    sq.resize((height, height), Image.NEAREST).save(path)
    return path


def setup(game, brand, login, out):
    if not brand or not login:
        raise SystemExit("stings need --brand and --login (from the channel identity)")
    os.makedirs(out, exist_ok=True)
    tag = "".join(c for c in game.lower() if c.isalnum()) or "game"
    ws.OUT = out
    ws.BRAND_TEXT = brand.upper()
    ws.TWITCH_TEXT = "TWITCH.TV/" + login.upper()
    ws.LOGO = wordmark_png(game, os.path.join(out, "_wordmark_%s.png" % tag))
    return tag


def intro(title, game, brand, login, out):
    """-> path of a 4.6 s intro with the title and no Part number."""
    tag = setup(game, brand, login, out)
    p = ws.make_intro(None, title)
    dst = os.path.join(out, "%s_intro_%s.mp4" % (tag, os.getpid()))
    os.replace(p, dst)
    return dst


def outro(game, brand, login, out):
    tag = setup(game, brand, login, out)
    return ws.make_outro(out_name="%s_outro.mp4" % tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["intro", "outro", "frames"])
    ap.add_argument("title", nargs="?", default="")
    ap.add_argument("--game", required=True)
    ap.add_argument("--brand", required=True)
    ap.add_argument("--login", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.what == "intro":
        print(intro(a.title, a.game, a.brand, a.login, a.out))
    elif a.what == "outro":
        print(outro(a.game, a.brand, a.login, a.out))
    else:
        setup(a.game, a.brand, a.login, a.out)
        ws.make_intro(None, a.title, frame_png=os.path.join(a.out, "_intro_frame.png"))
        o = ws.make_outro(out_name="_outro_preview.mp4", preset="veryfast", crf=22)
        ws.subprocess.run([ws.FF, "-v", "error", "-y", "-ss", "10", "-i", o, "-frames:v", "1",
                           os.path.join(a.out, "_outro_frame.png")], check=True)


if __name__ == "__main__":
    main()
