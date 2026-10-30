# ClipBot

TwinkiesGhost's VOD-to-Shorts lane. Runs entirely in the cloud; nothing on the laptop.

1. The Forge worker (`Irrelevant Forge/cloud/src/clipbot.js`) checks hourly at :30. When the
   stream is offline and a new VOD (20 min+, under 36 h old) exists, it starts `vod-clips`.
2. `vod-clips` downloads the VOD's audio only, transcribes it (whisper base.en), scores the
   funny moments, skips anything already clipped on Twitch, asks the worker's AI to pick the
   best 5 and title them, then downloads and renders just those 5 as vertical Shorts.
3. The worker DMs Leon each clip with Approve / Edit title + approve / Skip.
4. Approving queues it and starts `publish`, which uploads it to YouTube as Scheduled at the
   next free 18:00 UK day, then edits the DM with the link.
5. New Twitch clips (15 min old) are queued automatically and go out at 12:00 / 15:00 / 21:00 UK.

Slots: `clipbot/common.py` `VOD_SLOTS` / `CLIP_SLOTS`. Secrets live only in GitHub Actions
secrets and the worker's secrets.

Local test of the renderer: `py -m clipbot.render clip.mp4 outdir "HOOK TEXT"`.

## People (2026-10-29)
`CLIPBOT_WHO` picks whose lane a run is (`clipbot/common.py` `PEOPLE`): `leon` (default, everything
above, unchanged) or `glatiar` (Titan: his Twitch VODs/clips, his own approval channel, his
YouTube via secrets `GLAT_YT_CLIENT_ID` / `GLAT_YT_CLIENT_SECRET` / `GLAT_YT_REFRESH_TOKEN`). Workflows
take a `who` input; the scheduled publish runs both. Marathon mode, episodes and TikTok are Leon-only.
Tests: `py -m unittest discover -s tests -v`.
