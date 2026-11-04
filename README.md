# clip runner

Scheduled workflows that turn stream VODs and clips into short vertical videos and post them.
Everything that identifies an account (channel names and ids, the approval worker's address, tokens) lives
in repository secrets; nothing identifying is written in code, workflows, tests or logs.

Workflows: `vod-clips` and `clip-offer` render picks and send them for approval, `publish` schedules approved
clips, `instagram` and `tiktok` follow once a slot has passed, `episodes` cuts long-form parts.
Scheduled and chained runs only fire once the repository variable `GO` is `1`; a manual dispatch always runs.

Secrets: `CLIPBOT_ID` (JSON identity), `CLIPBOT_WORKER`, `CLIPBOT_TOKEN`, `CLIPBOT_ZIP_KEY`, the video-site and
Twitch app credentials, `TIKTOK_STATE`, `EP_PLAYLIST`.

Tests: `py -m unittest discover -s tests` (offline, synthetic identity from `tests/_ident.py`).
