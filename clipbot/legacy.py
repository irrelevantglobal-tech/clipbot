"""Rewrites the retired channel names (CLIPBOT_ID "legacy" list, never written in this repo) to the current one.

Clips that were approved or queued BEFORE the rebrand carry copy written with the old name (title suffix,
Twitch link, hashtag). They sit in the worker's queue and in already-rendered captions until their slot, so
every public text is passed through fix() on its way out. the owner's lane only: Glatiar's text is never touched.
Once the old queue has drained (every queued item is dated after the cutover) this module can be deleted
together with its call sites; `git grep -n legacy` lists them.
"""
import re

from . import common

_names = [n for n in common.IDENT.get("legacy", []) if n]
_OLD = re.compile("|".join(re.escape(n) for n in _names), re.I) if _names else None


def _new(m):
    s = m.group(0)
    if s.isupper():
        return common.BRAND.upper()
    return common.BRAND if s[0].isupper() else common.LOGIN


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def check(*texts, where=""):
    """Hard stop: refuse to post anything whose outgoing copy still names a retired channel, even split by spaces or
    punctuation ('old name', 'old_name'), or (Kick pivot, owner 18 Nov) still links Twitch. Raises, so that item is NOT
    posted (callers report it and move on)."""
    if common.WHO != "leon":
        return
    names = [_squash(n) for n in common.IDENT.get("legacy", []) if _squash(n)]
    for t in texts:
        sq = _squash(t)
        if any(n in sq for n in names):
            raise RuntimeError(f"{where or 'post'}: a retired channel name is still in the outgoing copy; NOT posted")
        if common.KICK_LIVE and _TWITCH_LINK.search(t or ""):
            raise RuntimeError(f"{where or 'post'}: the outgoing copy still links Twitch (Leon streams on Kick); NOT posted")


# Kick pivot (owner 18 Nov: "the twitch link removed and with the kick one on"): copy written before the pivot (queued
# Shorts, rendered captions, AI copy) gets Kick on its way out. The Twitch address becomes the Kick one (or the brand
# on Kick until KICK_SLUG_PUBLIC=1), Twitch hashtags become Kick ones, and the word Twitch becomes Kick.
_TWITCH_LINK = re.compile(r"(?:https?://)?(?:www\.)?twitch\.tv/[A-Za-z0-9_]+/?", re.I)
_TWITCH_TAGS = re.compile(r"#twitch(streamer|streaming|clips|live)?\b", re.I)
_TWITCH_WORD = re.compile(r"\btwitch\b", re.I)


def _case(m, word):
    s = m.group(0)
    return word.upper() if s.isupper() else (word.capitalize() if s[0].isupper() else word)


def kickify(text):
    if not common.KICK_LIVE or not text:
        return text
    url = common.live_url()
    # an approved caption's "Live on Twitch: <login>" line names the Kick address instead (Instagram links nothing)
    text = re.sub(r"(?i)\blive on twitch:\s*" + re.escape(common.LOGIN) + r"\b", "Live on Kick: " + (url or common.BRAND), text)

    def link(m):
        if not url:
            return common.live_text()
        return ("https://" if m.group(0).lower().startswith("http") else "") + url
    text = _TWITCH_LINK.sub(link, text)
    text = _TWITCH_TAGS.sub(lambda m: "#kick" + (m.group(1) or "").lower(), text)
    return _TWITCH_WORD.sub(lambda m: _case(m, "kick"), text)


def fix(text):
    if common.WHO != "leon" or not text:
        return text
    if _OLD is not None:
        text = _OLD.sub(_new, text)
    return kickify(text)
