"""Episode thumbnails (GTA 6 lane). Look = the style kit in clipbot/style (copied from
Documents\\_gta6-content\\style; Leon APPROVED the calm sunset version 2026-10-22: "Yes, use this").

    python -m clipbot.thumbs --vod 2904919965 --start 900 --end 4200 --title "I Drove Into The Sea" --ep 1 --out t.png

Frame: N one-second grabs spread over the episode's kept source range, straight from the Twitch VOD at
1080p (a few MB in all), scored for sharpness and light; dark, flat or near-blank frames (menus, loading,
fades) are rejected. Title: the episode hook; the template fits ~40 characters on 2 lines, so a longer hook
is shortened at a word boundary (never shrunk further: Leon called bigger/louder text "too much").
Any failure returns None: the episode keeps YouTube's own frame and scheduling is never blocked.
"""
import argparse
import os
import re
import subprocess
import sys

from .style import gta_style

N_GRABS = 8


def grab(vod, t, out):
    tmp = out + ".mp4"
    subprocess.run(["yt-dlp", "-q", "--no-progress", "-f", "1080p60/1080p/best[height<=1080]",
                    "--download-sections", f"*{t:.1f}-{t + 1.5:.1f}", "-o", tmp, f"https://www.twitch.tv/videos/{vod}"],
                   check=True, timeout=180)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.5", "-i", tmp, "-frames:v", "1", out], check=True)
    os.remove(tmp)
    return out


def score(path):
    """Sharpness x light. 0 = reject (too dark, too flat, blown out)."""
    from PIL import Image, ImageFilter, ImageStat
    im = Image.open(path).convert("L").resize((480, 270))
    st = ImageStat.Stat(im)
    mean, sd = st.mean[0], st.stddev[0]
    hist = im.histogram()
    p95 = next(i for i in range(256) if sum(hist[:i + 1]) >= 0.95 * sum(hist))
    if mean < 18 or p95 < 80 or sd < 20 or mean > 215:          # 2904919965: a dim face close-up is mean 21 / p95 92
        return 0.0
    edges = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).mean[0]
    return round(edges * min(1.0, sd / 50), 2)


def pick_frame(vod, spans, workdir):
    """Best of N_GRABS frames spread over the kept spans (5%..95% of kept time)."""
    total = sum(b - a for a, b in spans)
    best = None
    for k in range(N_GRABS):
        want = total * (0.05 + 0.9 * k / max(1, N_GRABS - 1))
        acc, t = 0.0, spans[0][0]
        for a, b in spans:
            if acc + (b - a) >= want:
                t = a + (want - acc)
                break
            acc += b - a
        p = os.path.join(workdir, f"cand{k}.png")
        try:
            grab(vod, t, p)
            s = score(p)
        except Exception as e:
            print(f"  thumb frame at {t:.0f}s failed: {str(e)[:120]}")
            continue
        print(f"  thumb frame {t:.0f}s score {s}")
        if s > 0 and (best is None or s > best[0]):
            best = (s, p, t)
    return best


def fit_title(hook, max_chars=40):
    words = re.sub(r"\s+", " ", hook).strip().split(" ")
    out = ""
    for w in words:
        if len((out + " " + w).strip()) > max_chars:
            break
        out = (out + " " + w).strip()
    small = {"a", "an", "the", "in", "on", "at", "of", "to", "and", "or", "for", "with", "my", "our", "into"}
    ws = out.split(" ")
    if out != hook.strip():
        while len(ws) > 2 and (ws[-1].lower() in small or (len(ws) > 2 and ws[-2].lower() in small)):
            ws = ws[:-1]                                     # "...On Day One In Vice" -> "...On Day One"
    return " ".join(ws) or hook[:max_chars]


def make(vod, spans, title, ep, series, out, workdir):
    os.makedirs(workdir, exist_ok=True)
    best = pick_frame(vod, spans, workdir)
    if not best:
        print("  no usable thumbnail frame")
        return None
    text = fit_title(title)
    while text:
        try:
            info = gta_style.episode_thumb(best[1], text, ep=ep, series=series, out=out, variant="sunset")
            print(f"  thumbnail: '{text}' from {best[2]:.0f}s -> {info.get('out', out)}")
            return info.get("out", out)
        except gta_style.TextOverflow:
            text = " ".join(text.split(" ")[:-1])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vod", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--ep", type=int)
    ap.add_argument("--series", default="GTA 6")
    ap.add_argument("--out", default="thumb.png")
    a = ap.parse_args()
    r = make(a.vod, [(a.start, a.end)], a.title, a.ep, a.series, a.out, os.path.splitext(a.out)[0] + "_work")
    print("RESULT", r)


if __name__ == "__main__":
    sys.exit(main())
