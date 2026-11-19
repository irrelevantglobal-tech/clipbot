"""Runner probe for clipbot/vision.py on clips already cut (vision-probe.yml): fetch each batch's encrypted release,
run the check on raw<n>.mp4 with its offered title + quote, print the verdicts."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from clipbot import common, publish, vision  # noqa: E402

total = 0.0
for spec in [s.strip() for s in sys.argv[1].split(",") if s.strip()]:
    batch, n = spec.rsplit(":", 1)
    d = os.path.abspath(os.path.join("work", batch))
    publish.fetch_artifact("0", common.release_name(batch), d)
    man = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    clip = next(c for c in man["clips"] if str(c["n"]) == n)
    r = vision.check(os.path.join(d, f"raw{n}.mp4"), clip["title"], clip.get("quote", ""), workdir=d)
    total += r["cost"]
    print(f"{batch}:{n} was {clip['title']!r} -> {r['verdict'].upper()} | {r['reason']} | title={r['title']!r} trim={r['trim']} ${r['cost']}")
print(f"vision cost this probe: ${total:.4f}")
