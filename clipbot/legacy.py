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


def fix(text):
    if common.WHO != "leon" or not text or _OLD is None:
        return text
    return _OLD.sub(_new, text)
