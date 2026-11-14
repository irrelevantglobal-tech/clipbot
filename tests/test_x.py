import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clipbot import common, x  # noqa: E402


def weight(t):
    link = f"https://twitch.tv/{common.LOGIN}"
    return len(t) - len(link) + x.URL_LEN


def test_text_is_fixed_and_fits():
    it = {"id": "vod:1:2", "title": "I Got Run Over By My Own Car", "tiktok_caption": "hook\n\n#gta6 #fyp #gtaonline #gaming"}
    a, b = x.x_text(it), x.x_text(it)
    assert a == b
    assert a.startswith("I Got Run Over By My Own Car\n\nLive on Twitch: https://twitch.tv/")
    assert "#gta6" in a and "#gtaonline" in a and "#fyp" not in a and "#gaming" not in a
    assert weight(a) <= 280


def test_long_title_drops_tags_then_trims():
    it = {"id": "vod:1:3", "title": "W" * 400, "tiktok_caption": "#one #two #three"}
    t = x.x_text(it)
    assert weight(t) <= 280 and "Live on Twitch" in t


def test_already_matches_on_title_line():
    it = {"id": "vod:1:2", "title": "Stone Ruins His Life"}
    text = x.x_text(it)
    posts = [{"id": "a", "text": "Something else\n\nLive on Twitch"}, {"id": "b", "text": text, "status": "sent"}]
    assert x.already(posts, text)["id"] == "b"
    assert x.already(posts[:1], text) is None


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
