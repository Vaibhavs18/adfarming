# Build log (new work only — Meta hackathon rule)

| Date | What was built |
|---|---|
| 2026-10-27 | Repo scaffold: README, MIT license, agent-contract and trace-record JSON schemas |
| 2026-11-03 | Concept engine: Muse Spark Open (Baseten) -> 3 concepts as JSON, schema-validated trace and contract JSONL; runner script |
| 2026-11-03 | Verified Meta Model API (Spark 1.3, Image 1.0, Voice Transcribe 1.0) with hackathon key; Voice Transcribe handles Hindi/Hinglish on client clips, replaces Whisper test |
| 2026-11-03 | V1 pipeline: Spark 1.3 concepts with real cut points (Voice Transcribe turns), human approval gate, Muse Image storyboard, ffmpeg 9:16/1:1/16:9 cuts with burned-in Hinglish captions, static review page with full trace |
| 2026-11-03 | V1.1: SAM 3.1 speaker tracking drives 1:1 crop; Muse Wonder QC agent with auto re-edit loop (Spark revises cut from QC issues); cuts snap to speech boundaries, dangling words trimmed, speech-timed Roman-Hinglish captions (Spark), hook text + CTA end hold; storyboard labelled AI stand-in |
| 2026-11-03 | T1 QA hardening: QC key frames now include the CTA end hold, best-scoring QC round is delivered (per-round dirs, early stop), SAM/QC failures degrade instead of crashing, robust JSON extraction and validation, bundled Noto Sans fonts (OFL) for Hinglish captions, requirements.txt, Dockerfile, pure-function pytest suite |
| 2026-11-03 | Studio HTTP backend (T4): FastAPI `adfarming.web:app` implementing docs/API_CONTRACT.md, `runner.py` AgentRunner interface + scripted StubRunner, 54 tests in tests/web |
| 2026-11-03 | T3 agent: per-step stateless Wonder tool loop (WonderDriver) with ScriptedDriver per-turn fallback, code-built gate cards, code-enforced paid-tool gates and cost cap, concepts.generate/revise accept human `guidance`, scripted-human real e2e script |
