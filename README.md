# AdFarming

See your ad before you pay for it. You give the Studio a brief and some footage; it writes three ad ideas, sketches a storyboard for the one you pick, and only then builds the finished cuts. You approve every paid step, and every decision is logged.

Built during the Meta Global AI Developer Hackathon (Oct 26 - Nov 4, 2026) and extended for the Vultr Agent Rush (Nov 3-8, 2026). MIT licensed.

## Try it

**[adfarming-studio-preview.vercel.app](https://adfarming-studio-preview.vercel.app/)** is a clickable demo with a made-up brand ("Demo Saree Brand"). It runs entirely in your browser: no server, no AI, nothing is spent. Type any password to get in, press **New run**, then **Start**.

Skip ahead to a step with `?mock=1&at=ideas`, `preview`, `edit`, `check` or `ship`. A demo of a real session on a real client exists too; ask the team for the private link.

## How to use the Studio, step by step

You do not need to know any code. Think of the Studio as a junior creative who does the legwork and stops at six checkpoints to ask you. Nothing moves forward without your click.

**Before you start.** The left panel is the client's **memory**: the rules and preferences the agent follows every time (tone of voice, things never to say, what worked before). A person approves everything in it.

**The six steps** (the bar across the top shows where you are):

1. **Brief.** The Studio restates what it understood. Read it. Is it the ad you want? Press **Approve** if yes. If something is off, use **Edit** to fix the words yourself, or **Steer** to tell it in your own words what to change.
2. **Ideas.** Three concepts, each with a hook (the opening line), what happens beat by beat, why it fits this client, what to watch out for, and which memory rules it obeys. One is marked *Recommended*. Press **Approve** on the one you want. Not quite right? Type in the Steer box, for example "c2 but a shorter hook". You can also **Edit** the hook or the call to action directly.
3. **Preview.** Rough storyboard frames for your chosen idea. This is the cheap moment to say no. The button shows what the next step will cost. **Approve** to build the real cuts, **Steer** to change the idea, or **Back** to Ideas.
4. **Edit.** The finished cuts in three sizes: 9:16 (Reels, Shorts, Stories), 1:1 (feed), 16:9 (YouTube, web), with captions. Play them. You can edit the hook and the call to action. To change a caption word, use Steer: the caption words come from the speech and are not typed over by hand.
5. **Check.** A quality check that "watches" the cut like a client would and gives a score out of 5, what is wrong, and suggested fixes. Tick the fixes you want and press **Steer: apply fixes**, or **Approve anyway** if you disagree with the check. The check is an opinion, not a verdict: you decide.
6. **Ship.** The three cuts and a zip, plus **What I learned this run**: things the agent noticed from your steers (for example "prefers shorter hooks"). **Accept** adds one to the client memory; **Reject** throws it away. Nothing is learned silently.

**The buttons.**
- **Approve** moves to the next step. Where money is about to be spent, the button says how much.
- **Steer** sends a note in your own words; the agent redoes that step and asks again.
- **Edit** changes the text yourself; your edit is final.
- **Back** returns to an earlier step. Later steps are then marked as out of date and are redone if you go on.
- **Stop** ends the run.

**Reading the screen.**
- **Awaiting you** means the Studio has stopped and is waiting for your click. **Working** means it is busy; you cannot do anything yet and the screen updates by itself.
- **Spent $x / Cap $y** (top) is what this run has cost so far against a hard limit. Hover over it to see the cost per tool.
- **Memory in play** (strip above the steps) lists the rules this step is using. In the left panel, rules marked *In play* are the ones used right now; *Pinned* ones are the hard rules; *Learned by agent* means the agent suggested it and a person accepted it; *Proposed* means it is waiting for your Accept or Reject. Every rule shows where it came from.
- **What Wonder is doing** (right side) is a plain-language running commentary.
- **Decision log** lists every decision in order: who did it (you, the agent, or the system), at which step, what it cost, and which memory rules it relied on. It is append-only, so it can be reviewed later.

**What is NOT automated (honestly).**
- It does not post, schedule, buy media or touch your ad account. You download the cuts and upload them yourself.
- You supply the footage and the brief. It cuts and captions what you give it; it does not shoot or invent footage, and the storyboard frames are sketches, not the final video.
- The quality check is a second opinion from an AI and can be wrong in both directions. A person should always watch the cut.
- It does not look at live ad results by itself. What worked before reaches it only because a person put it in the memory.
- Captions come from speech recognition of Hindi/Hinglish and may need a human read before they go out.

## Run it yourself

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export STUDIO_PASSWORD='choose-one' STUDIO_SECRET="$(openssl rand -hex 32)" STUDIO_RUNNER=stub
.venv/bin/python -m uvicorn adfarming.web:app --app-dir src --port 8000
# open http://localhost:8000
```

- `STUDIO_LOOK=composed` (default) or `simple` picks the reel look; in the browser demo add `&look=simple` to `?mock=1` for the old cards.
- `STUDIO_RUNNER=stub` is a scripted fake: nothing is spent, no keys needed. `STUDIO_RUNNER=real` uses the real agent and the paid model APIs, so it needs the keys in `deploy/.env.example` (names only; put values in a local `.env`, never commit it).
- Other settings, all listed in `deploy/.env.example`: `STUDIO_AGENT`, `STUDIO_RUN_CAP_USD`, `STUDIO_DAILY_CAP_USD`, `STUDIO_CLIENT_ID`, `STUDIO_CLIENT_NAME`, `STUDIO_MEMORY_SEED`, `ADFARMING_DB`, `ADFARMING_OUT`.
- Tests: `.venv/bin/python -m pytest -q` (no network, no keys).
- Another client: see `docs/CLIENT_SWAP.md`. API shapes: `docs/API_CONTRACT.md`. Static demo modes: `web/README.md`.

## Deploy

One VM with Docker Compose, HTTPS and a password gate: follow `deploy/README.md`. Client footage and run data stay on the server's disk, never in git.

## What is inside

1. **Concept engine** (Muse Spark): brief + client memory -> three concepts with real cut points taken from the footage's transcript.
2. **Look preview** (Spark plan, SAM 3.1 still cut-out, Muse Image plate): stills of the real speaker cut out over planned backgrounds, with stylised text, which the client approves (steers, edits) before any video is rendered. `STUDIO_LOOK=simple` keeps the old Muse Image storyboard and the plain talking-head cut as the fallback look.
3. **Composed cuts** (ffmpeg, SAM 3.1 video matte, Muse Voice Transcribe): 9:16 / 1:1 / 16:9 with the speaker cut out over changing backgrounds and Hinglish captions; the simple talking-head cut is built too and is the fallback.
4. **Quality check** (Muse Wonder): scores the cut and proposes fixes; the best round is delivered.
5. **Agent and memory** (`src/adfarming/agent`, `studio`): one step at a time, paid tools only after a human gate, a hard spend cap, and a decision log of every step (schemas in `schemas/`).

## What was built during the hackathon
Everything in this repo. See `docs/BUILD_LOG.md` for a dated log; the first commit is Oct 27, 2026.

## License
MIT
