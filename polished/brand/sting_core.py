"""Episode intro sting + end-screen outro (the WD2 sting code, made channel-neutral 2026-11-12).

The channel name and Twitch login come from the caller (BRAND_TEXT / TWITCH_TEXT, set by stings.py
from the channel identity), never from this file: the repo is public. The intro shows the episode
title but NO Part number when ep_num is None, because Part numbers are given at approval.

Same synthwave brand as the WD1 / Legion series (channel consistency). Two things are
deliberately different from wd1_stings.py:

1. The title is the game's own WD2 wordmark PNG (brand/assets/wd2_logo_white.png),
   not gradient text. That mark is roughly SQUARE (797x774) where WD1's was wide, so it
   is ALWAYS scaled by HEIGHT with aspect preserved - never to a target width.
2. The outro plates are drawn AT YouTube's measured end-screen element MINIMUMS
   (video 620x349, subscribe 296x296 - see brain/youtube-endscreens-and-pins.md).
   WD1's 460x259 plates were smaller than the smallest element YouTube allows, so the
   element had to enclose and hide the plate. Here the real elements drop exactly onto
   the plates, and every label / the wordmark / the Twitch line sits OUTSIDE those zones.

Audio is numpy-synthesised, so there is no copyright exposure, and it gets matched DOWN
to the raw VOD level at render time by wd2_render_all.py (loudnorm=I=-34:TP=-3:LRA=11),
never the body up to it.

Durations are a CONTRACT: intro 4.6 s / outro 20.0 s must match INTRO_D/OUTRO_D in
wd2_render_all.py + wd2_emit_metadata.py (check_sting aborts on more than 0.20 s drift).

    py -V:3.12 wd2_stings.py intro <n> "<episode title>"
    py -V:3.12 wd2_stings.py outro
    py -V:3.12 wd2_stings.py test      # CPU-only test render + eyeball frames
"""
import math
import os
import shutil
import subprocess
import sys
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brandkit as bk  # noqa: E402

OUT = os.environ.get("STING_OUT", ".")
LOGO = os.path.join(OUT, "wordmark.png")
FF = shutil.which("ffmpeg") or "ffmpeg"
BRAND_TEXT = "CHANNEL"          # set by stings.py
TWITCH_TEXT = "TWITCH.TV/CHANNEL"
FPS, SR = 60, 48000


# --------------------------------------------------------------------- audio
def adsr(n, a, d, r, sus=0.7):
    env = np.ones(n)
    ai, di, ri = int(a * SR), int(d * SR), int(r * SR)
    if ai:
        env[:ai] = np.linspace(0, 1, ai)
    if di:
        env[ai:ai + di] = np.linspace(1, sus, di)
    env[ai + di:n - ri] = sus
    if ri:
        env[n - ri:] = np.linspace(sus, 0, ri)
    return env


def saw(f, n, detune=0.0):
    t = np.arange(n) / SR
    out = np.zeros(n)
    for k in range(1, 12):
        out += np.sin(2 * np.pi * f * (1 + detune) * k * t) / k
    return out * 0.5


def note(f, dur, detune=0.006):
    n = int(dur * SR)
    w = (saw(f, n, 0) + saw(f, n, detune) + saw(f, n, -detune)) / 3
    return w * adsr(n, 0.005, 0.06, 0.12)


SEQ = [0, 3, 7, 10, 12, 15, 19, 24]
STEP = 0.115


def intro_audio(dur):
    n = int(dur * SR)
    mix = np.zeros(n)
    for i, sd in enumerate(SEQ):
        w = note(110.0 * (2 ** (sd / 12)), STEP * 2.2)
        st = int(i * STEP * SR)
        en = min(n, st + len(w))
        mix[st:en] += w[:en - st] * 0.16
    land = int((len(SEQ) * STEP) * SR)
    bn = min(n - land, int(1.6 * SR))
    if bn > 0:
        t = np.arange(bn) / SR
        mix[land:land + bn] += np.sin(2 * np.pi * np.cumsum(np.linspace(90, 42, bn)) / SR) \
            * np.exp(-t * 2.2) * 0.5
        mix[land:land + bn] += (np.sin(2 * np.pi * 880 * t) + np.sin(2 * np.pi * 1320 * t)) \
            * np.exp(-t * 5.0) * 0.05
    sw = int(0.9 * SR)
    s0 = max(0, land - sw)
    nz = np.random.default_rng(3).normal(0, 1, land - s0)
    mix[s0:land] += nz * (np.linspace(0, 1, land - s0) ** 3) * 0.05
    return np.tanh(mix * 1.4) * 0.8


def outro_audio(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    mix = np.zeros(n)
    chords = [[220.0, 261.63, 329.63], [174.61, 220.0, 261.63],
              [261.63, 329.63, 392.0], [196.0, 246.94, 293.66]]
    seg = dur / len(chords)
    for ci, ch in enumerate(chords):
        st, en = int(ci * seg * SR), int(min(dur, (ci + 1) * seg) * SR)
        tt = np.arange(en - st) / SR
        env = np.clip(np.minimum(1, tt / 0.6) * np.minimum(1, (seg - tt) / 0.8), 0, 1)
        for f in ch:
            mix[st:en] += (np.sin(2 * np.pi * f * tt) * 0.6
                           + np.sin(2 * np.pi * f * 2 * tt) * 0.12) * env * 0.09
    mix *= (0.88 + 0.12 * np.sin(2 * np.pi * 0.5 * t))
    mix = np.tanh(mix * 1.2) * 0.6
    fo = int(1.2 * SR)
    mix[-fo:] *= np.linspace(1, 0, fo)
    return mix


def write_wav(path, mono):
    pcm = (np.clip(np.stack([mono, mono], axis=1), -1, 1) * 32767).astype(np.int16)
    w = wave.open(path, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
    w.close()


# -------------------------------------------------------------------- layers
def logo_layer(height, glow=(127, 255, 212), glow_r=14, glow_a=0.5):
    """The WD2 wordmark at a given HEIGHT, white with the brand's mint rim glow.

    The mark is roughly SQUARE (797x774): scale by height only, aspect preserved.
    WD1's wide-mark maths (scale-to-a-fraction-of-frame-WIDTH) do not transfer - at
    40% of frame width this mark would be 745 px tall and swallow the whole frame.
    """
    src = Image.open(LOGO).convert("RGBA")
    w = max(1, round(src.width * height / src.height))
    mark = src.resize((w, height), Image.LANCZOS)
    pad = glow_r * 3
    layer = Image.new("RGBA", (w + pad * 2, height + pad * 2), (0, 0, 0, 0))
    a = Image.new("L", layer.size, 0)
    a.paste(mark.getchannel("A"), (pad, pad))
    g = a.filter(ImageFilter.GaussianBlur(glow_r))
    gl = Image.new("RGBA", layer.size, glow + (0,))
    gl.putalpha(g.point(lambda v: int(min(255, v * glow_a))))
    layer.alpha_composite(gl)
    layer.alpha_composite(mark, (pad, pad))
    return layer


def title_layer(lines, size=150, tracking=4):
    pad = 260
    fnt = bk.font("ariblk.ttf", size)
    wmax = int(max(fnt.getlength(l) + tracking * (len(l) - 1) for l in lines)) + pad
    lh = int(size * 1.06)
    img = Image.new("RGBA", (wmax, lh * len(lines) + pad), (0, 0, 0, 0))
    for i, l in enumerate(lines):
        bk.gradient_text(img, l, fnt, wmax // 2, pad // 2 + i * lh, tracking=tracking,
                         glow_r=18, glow_a=1.0)
    return img


def paste_scaled(base, layer, cx, cy, scale, alpha):
    if alpha <= 0.01 or scale <= 0.01:
        return
    w, h = max(1, int(layer.width * scale)), max(1, int(layer.height * scale))
    lr = layer.resize((w, h), Image.LANCZOS)
    if alpha < 1.0:
        lr.putalpha(lr.getchannel("A").point(lambda v: int(v * alpha)))
    base.alpha_composite(lr, (int(cx - w / 2), int(cy - h / 2)))


def encode(frames_dir, wav, out, preset="slow", crf=16):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                    "-framerate", str(FPS), "-i", os.path.join(frames_dir, "%05d.png"),
                    "-i", wav, "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(SR),
                    "-shortest", out], check=True)


# --------------------------------------------------------------------- intro
def make_intro(ep_num, ep_title, dur=4.6, frame_png=None):
    # ⚠️ The frames dir and the wav MUST be unique per episode - wd2_stings_batch.py runs
    # several of these in parallel, and a shared "_f_intro" would interleave PNG frames
    # from different episodes into one sting (same trap as the contact-sheet _tiles/ dir).
    big = logo_layer(430)
    small = bk.font("arial.ttf", 30)
    pillf = bk.font("arialbd.ttf", 27)
    land = 0.92 * len(SEQ) * STEP

    def draw(t):
        img = bk.background(grid_offset=(t * 0.10) % 1.0, twinkle=t * 2.4).convert("RGBA")
        if t < land:
            p = t / land
            sc, al = 1.35 - 0.30 * (p ** 0.6), min(1.0, p * 1.8)
        else:
            q = min(1.0, (t - land) / 0.45)
            sc, al = 1.05 - 0.05 * (1 - (1 - q) ** 3), 1.0
        paste_scaled(img, big, 960, 460, sc, al)

        if t > land - 0.1:
            a = min(1.0, (t - (land - 0.1)) / 0.5)
            bk.plain_text(img, BRAND_TEXT, small, 960, 130, tracking=16, alpha=0.72 * a)
            if ep_num is not None:
                bk.plain_text(img, "PART %02d" % ep_num, pillf, 960, 758, tracking=8,
                              color=(127, 255, 212), alpha=0.95 * a)
            if ep_title:   # without a Part line the title takes its place, in the Part line's colour
                bk.plain_text(img, ep_title.upper(), small if ep_num is not None else pillf, 960,
                              806 if ep_num is not None else 770, tracking=5,
                              color=(200, 245, 235) if ep_num is not None else (127, 255, 212),
                              alpha=0.65 * a if ep_num is not None else 0.95 * a)
        if 0 <= t - land < 0.16:
            fa = (1 - (t - land) / 0.16) ** 2
            img.alpha_composite(Image.new("RGBA", (bk.W, bk.H), (200, 255, 240, int(150 * fa))))
        if t > dur - 0.35:
            k = (t - (dur - 0.35)) / 0.35
            img.alpha_composite(Image.new("RGBA", (bk.W, bk.H), (0, 0, 0, int(255 * k))))
        return img

    if frame_png:
        # eyeball path: ONE representative frame (logo landed, all text up), no encode
        draw(4.1).convert("RGB").save(frame_png)
        print("intro frame ->", frame_png)
        return frame_png

    tag = "%02d" % ep_num if ep_num is not None else "x%d" % os.getpid()
    d = os.path.join(OUT, "_f_intro_ep%s" % tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    n = int(dur * FPS)
    for i in range(n):
        draw(i / FPS).convert("RGB").save(os.path.join(d, "%05d.png" % (i + 1)))

    wav = os.path.join(OUT, "_intro_ep%s.wav" % tag)
    write_wav(wav, intro_audio(dur))
    out = os.path.join(OUT, "intro_ep%s.mp4" % tag)
    encode(d, wav, out)
    shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(wav):
        os.remove(wav)
    print("intro ->", out)
    return out


# --------------------------------------------------------------------- outro
# End-screen zones at YouTube's measured element MINIMUMS (they cannot be shrunk below
# this, so plates any smaller get hidden under the element - the WD1 lesson). Everything
# else on the frame stays OUT of these two rects:
#   video    620x349 at (120,400)  -> spans x 120..740,  y 400..749
#   subscribe 296x296, centre (960,574), r 148 -> spans x 812..1108, y 426..722
BOX_W, BOX_H = 620, 349
BOX_V = (120, 400)
SUB_C, SUB_R = (960, 574), 148
# wordmark sits right of both zones: height 340 -> ~350 wide, centred (1550,560)


def make_outro(dur=20.0, out_name="wd2_outro.mp4", preset="slow", crf=16):
    stem = os.path.splitext(out_name)[0]
    d = os.path.join(OUT, "_f_" + stem)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    n = int(dur * FPS)

    head = title_layer(["THANKS FOR WATCHING"], 96, tracking=3)
    mark = logo_layer(340)
    lab = bk.font("arialbd.ttf", 24)
    soc = bk.font("arial.ttf", 26)
    han = bk.font("ariblk.ttf", 40)

    for i in range(n):
        t = i / FPS
        img = bk.background(grid_offset=(t * 0.10) % 1.0, twinkle=t * 2.0).convert("RGBA")
        paste_scaled(img, head, 960, 168, 1.0, min(1.0, t / 0.6))

        ov = Image.new("RGBA", (bk.W, bk.H), (0, 0, 0, 0))
        dr = ImageDraw.Draw(ov)
        pulse = 0.55 + 0.25 * math.sin(t * 2.2)
        bx, by = BOX_V
        dr.rectangle([bx, by, bx + BOX_W, by + BOX_H], fill=(8, 14, 30, 165),
                     outline=(127, 255, 212, int(255 * pulse)), width=3)
        dr.ellipse([SUB_C[0] - SUB_R, SUB_C[1] - SUB_R, SUB_C[0] + SUB_R, SUB_C[1] + SUB_R],
                   fill=(8, 14, 30, 165), outline=(83, 252, 24, int(255 * pulse)), width=3)
        img.alpha_composite(ov)

        a = min(1.0, max(0.0, (t - 0.35) / 0.6))
        paste_scaled(img, mark, 1550, 560, 1.0, a)
        bk.plain_text(img, "NEXT EPISODE", lab, BOX_V[0] + BOX_W // 2, BOX_V[1] + BOX_H + 18,
                      tracking=5, color=(180, 240, 225), alpha=0.85 * a)
        bk.plain_text(img, "SUBSCRIBE", lab, SUB_C[0], SUB_C[1] + SUB_R + 20,
                      tracking=5, color=(83, 252, 24), alpha=0.9 * a)
        # TWITCH-FIRST by standing rule: the outro's one CTA is the Twitch channel.
        # No Kick anywhere on screen, no em dashes in any string.
        bk.plain_text(img, "CATCH THE STREAMS LIVE AT", soc, 960, 846,
                      tracking=7, color=(180, 240, 225), alpha=0.6 * a)
        bk.gradient_text(img, TWITCH_TEXT, han, 960, 888, tracking=5,
                         glow_r=16, glow_a=0.5, hard_shadows=((3, 3, (5, 30, 25), 0.85),))

        if t > dur - 0.8:
            k = (t - (dur - 0.8)) / 0.8
            img.alpha_composite(Image.new("RGBA", (bk.W, bk.H), (0, 0, 0, int(255 * k))))
        img.convert("RGB").save(os.path.join(d, "%05d.png" % (i + 1)))

    wav = os.path.join(OUT, "_" + stem + ".wav")
    write_wav(wav, outro_audio(dur))
    out = os.path.join(OUT, out_name)
    encode(d, wav, out, preset=preset, crf=crf)
    shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(wav):
        os.remove(wav)
    print("outro ->", out)
    return out


# ---------------------------------------------------------------------- test
def make_test():
    """CPU-only proof render: the outro (where the layout risk lives) encoded at
    veryfast to _test_sting.mp4, one frame pulled from the mp4 itself, and one
    intro frame drawn straight to PNG (no second video encode). Never NVENC here."""
    out = make_outro(out_name="_test_sting.mp4", preset="veryfast", crf=20)
    png = os.path.join(OUT, "_test_sting_frame.png")
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", "10",
                    "-i", out, "-frames:v", "1", png], check=True)
    print("outro frame ->", png)
    make_intro(1, "California Sound", frame_png=os.path.join(OUT, "_test_intro_frame.png"))


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "both"
    if what in ("intro", "both"):
        make_intro(int(sys.argv[2]) if len(sys.argv) > 2 else 1,
                   sys.argv[3] if len(sys.argv) > 3 else "")
    if what in ("outro", "both"):
        make_outro()
    if what == "test":
        make_test()
