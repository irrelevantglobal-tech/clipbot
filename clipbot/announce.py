"""One-off launch-night announcements (Kick pivot, 18 Nov): the "TONIGHT on Kick" X post and the 2 Instagram Story
slides, from an encrypted release (clips-<batch>: announce_x.txt, story1_tonight.jpg, story2_founder.jpg).

    python -m clipbot.announce --batch <B> --x [--dry]          post announce_x.txt to X now (Buffer), read back the link
    python -m clipbot.announce --batch <B> --stories [--dry]    post the story*.jpg files as Instagram Stories, in order

Same guards as the lanes: Leon's lane only; legacy.check refuses any outgoing text that links Twitch or names a retired
channel; X skips a text Buffer already has (no double post on a re-run). --dry builds everything and posts nothing.
"""
import argparse
import glob
import os
import sys

from . import common, ep_story, instagram, legacy, publish, x


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--x", action="store_true")
    ap.add_argument("--stories", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if common.WHO != "leon":
        raise SystemExit("Leon's lane only. Nothing posted.")
    work = os.path.abspath("work")
    d = os.path.join(work, "announce")
    publish.fetch_artifact("0", common.release_name(a.batch), d)
    if a.x:
        text = open(os.path.join(d, "announce_x.txt"), encoding="utf-8").read().strip()
        legacy.check(text, where="announce X")
        print(f"X text ({len(text)} chars):\n{text}\n")
        key = os.environ.get("BUFFER_API_KEY", "")
        if not key:
            raise SystemExit("no Buffer key: nothing posted to X")
        org, chan = x.x_channel(key)
        same = x.already(x.recent_posts(key, org, chan["id"]), text)
        if same:
            print(f"X: already in Buffer ({same['status']}) {same.get('externalLink') or same['id']}: not posted again")
        elif a.dry:
            print("X: DRY, not posted")
        else:
            pid = x.create(key, chan["id"], text)
            print(f"X: posted {pid} {x.wait_sent(key, pid) or '(still sending in Buffer)'}")
    if a.stories:
        tok = instagram.login()
        for path in sorted(glob.glob(os.path.join(d, "story*.jpg"))):
            mid = ep_story.post_story(tok, path, a.dry)
            print(f"Instagram Story {os.path.basename(path)}: {'DRY, container FINISHED' if a.dry else 'posted ' + str(mid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
