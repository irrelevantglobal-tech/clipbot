"""Synthetic channel identity for the tests: the real one lives only in the CLIPBOT_ID secret (public repo)."""
import json
import os

LOGIN, BRAND, UID, IG = "ownerlogin", "OwnerBrand", "4242", "ownerig"
OLD = "OldName"          # a retired name that queued copy may still carry
os.environ["CLIPBOT_ID"] = json.dumps({"login": LOGIN, "brand": BRAND, "twitch_uid": UID,
                                       "channel": "UCownerTESTchannel0000", "ig": IG, "legacy": [OLD]})
