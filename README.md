# AdFarming — see your ad before you pay for it

Brief → 3 written ad concepts → approved storyboard → platform-ready cuts, with a reviewable log of every agent decision.

Built during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026) and extended for the Vultr Agent Rush (Nov 3–8, 2026).

## Pipeline
1. **Concept engine** — Muse Spark: brief + niche signals (long-running ads) → 3 concepts (hook, script, shot list, why it fits).
2. **Storyboard preview** — Muse Image: chosen concept → frames the client approves before any video spend.
3. **Footage path** — SAM 3.1 keeps the product framed across 9:16 / 1:1 / 16:9; Muse Voice Transcribe adds subtitles.
4. **Decision log** — every agent step is a JSON contract + trace record (see `schemas/`).
5. **Handoff** — package staged as a draft for Meta Ads Manager (if MCP access works).

## What was built during the hackathon
Everything in this repo. See `docs/BUILD_LOG.md` for a dated log; the first commit is Oct 27, 2026.

## Status
Scaffold only. Day 1 of the build.

## License
MIT
