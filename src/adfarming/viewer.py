"""Review page for humans (client, judges): the run told as a story, plain language first, raw trace last."""
import html
import json
from pathlib import Path

from . import clients

CSS = """
:root{--bg:#fbfaf8;--fg:#1b1d21;--mut:#5f6673;--card:#fff;--line:#e6e3dd;--acc:#4f46e5;--ok:#15803d;--bad:#b42318;
--okbg:#e8f5ec;--badbg:#fdecea;--soft:#f3f1ec}
@media(prefers-color-scheme:dark){:root{--bg:#111214;--fg:#eceae6;--mut:#a4a9b4;--card:#1a1b1e;--line:#2c2e33;--acc:#8b93ff;
--ok:#5ad28a;--bad:#ff8b7e;--okbg:#14301f;--badbg:#3a1a17;--soft:#202125}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:980px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:28px;margin:0 0 6px}h2{font-size:21px;margin:44px 0 6px}h3{font-size:17px;margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 16px;max-width:70ch}
.step{display:inline-block;background:var(--acc);color:#fff;border-radius:99px;font-size:13px;padding:1px 10px;margin-right:8px;vertical-align:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.stats{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:16px 0}
.stat{background:var(--soft);border-radius:12px;padding:12px 14px}.stat b{display:block;font-size:22px}
.stat span{color:var(--mut);font-size:13px}
.pill{display:inline-block;font-size:12px;border-radius:99px;padding:2px 10px;margin:0 6px 6px 0;background:var(--soft);color:var(--mut)}
.pick{border-color:var(--acc);box-shadow:0 0 0 2px var(--acc) inset}
.badge{font-size:12px;background:var(--acc);color:#fff;border-radius:99px;padding:2px 10px;float:right}
.said{background:var(--soft);border-radius:10px;padding:10px 12px;margin:8px 0;font-size:15px}
.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.verdict{border-radius:14px;padding:16px;background:var(--okbg);border:1px solid var(--ok)}
.verdict.fail{background:var(--badbg);border-color:var(--bad)}
.videos{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end}.videos figure{margin:0}
figcaption{font-size:13px;color:var(--mut);margin-top:4px}
video,img{border-radius:10px;background:#000;max-width:100%}
.frames{display:flex;gap:10px;flex-wrap:wrap}.frames img{height:260px}
.model{display:flex;gap:10px;align-items:flex-start}.model b{min-width:150px}
ol.log{padding-left:0;list-style:none;margin:0}ol.log li{padding:8px 0;border-bottom:1px solid var(--line)}
ol.log .who{font-size:12px;color:var(--mut);display:block}
details{margin-top:14px}summary{cursor:pointer;color:var(--acc);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:10px}td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
.ask{background:var(--soft);border-radius:12px;padding:14px 16px}.ask li{margin:4px 0}
@media(max-width:600px){main{padding-top:18px}h1{font-size:23px}.frames img{height:190px}}
"""

STAGE = {  # tool -> (stage, plain label)
    "muse-voice-transcribe": ("Listen", "Meta Muse Voice Transcribe"),
    "muse-spark": ("Think", "Meta Muse Spark"),
    "human-approval": ("Decide", "A person"),
    "muse-image": ("Preview", "Meta Muse Image"),
    "snap-to-speech": ("Edit", "Edit step"),
    "trim-dangling": ("Edit", "Edit step"),
    "ffmpeg": ("Edit", "Video tool (ffmpeg)"),
    "sam-3.1": ("Frame", "Meta SAM 3.1"),
    "muse-wonder": ("Check", "Meta Muse Wonder"),
}
MODELS = [  # (tool key, name, plain job)
    ("muse-voice-transcribe", "Muse Voice Transcribe", "Listens to the footage and writes down what was said, with timing, in Hindi and English."),
    ("muse-spark", "Muse Spark", "Writes the ad ideas, fixes the edit after a failed check, and cleans up the captions."),
    ("muse-image", "Muse Image", "Draws a preview frame for each beat so the idea can be judged before editing."),
    ("sam-3.1", "SAM 3.1", "Follows the speaker through the video so square and wide cuts keep her in frame."),
    ("muse-wonder", "Muse Wonder", "Acts as the quality checker: reads the finished cut and says pass or fail, and why."),
]
RATIO = {"9x16": "Vertical 9:16 · Reels and Stories", "1x1": "Square 1:1 · Feed", "16x9": "Wide 16:9 · YouTube and desktop"}


def e(x) -> str:
    return html.escape(str(x))


def _secs(ms: int) -> str:
    s = ms / 1000
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f} min"


def render(run_dir: Path, picked: str | None, meta: dict, client_id: str | None = None) -> Path:
    concepts = json.loads((run_dir / "concepts.json").read_text())
    trace = [json.loads(l) for l in (run_dir / "trace.jsonl").read_text().splitlines() if l.strip()]
    cdir = clients.client_dir(client_id)
    brief = clients.load_brief(client_id)
    asr = {p.stem: json.loads(p.read_text()) for p in sorted((cdir / "asr").glob("*.json"))} if (cdir / "asr").exists() else {}
    used = {t["tool"] for t in trace} | ({"muse-voice-transcribe"} if asr else set())
    total_ms, cost = sum(t["duration_ms"] for t in trace), sum(t.get("cost_usd", 0) for t in trace)
    final = next((c for c in concepts if c["id"] == picked), None)
    hist = meta.get("qc_history", [])
    q = meta.get("qc")

    o = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>AdFarming ad review</title><style>{CSS}</style><main>"]

    # ---- summary
    o.append("<h1>AdFarming: from a brief to a ready-to-review ad</h1>"
             "<p class=lead>A person gives AdFarming a brief and raw footage. Meta's AI models write the ideas, "
             "preview them, edit the video into three shapes, add captions, and check their own work. "
             "A person approves the idea before anything is built. This page shows everything that happened, in order.</p>")
    o.append("<div class=stats>"
             f"<div class=stat><b>{len(asr) or '–'} clips</b><span>raw footage in</span></div>"
             f"<div class=stat><b>3 ideas</b><span>written by AI</span></div>"
             f"<div class=stat><b>{len(meta.get('cuts', {}))} versions</b><span>of the final ad</span></div>"
             f"<div class=stat><b>{_secs(total_ms)}</b><span>machine time</span></div>"
             f"<div class=stat><b>${cost:.2f}</b><span>API cost of this run</span></div></div>")

    # ---- models strip
    o.append("<h2>Which Meta models did what</h2><div class=card>")
    for key, name, job in MODELS:
        n = sum(1 for t in trace if t["tool"] == key)
        badge = f"{n} calls" if n else ("used" if key in used else "not used in this run")
        o.append(f"<div class=model><b>{e(name)}</b><div>{e(job)} <span class=pill>{e(badge)}</span></div></div>")
    o.append("</div>")

    # ---- 1 brief
    o.append("<h2><span class=step>1</span>The brief</h2><p class=lead>What the client wants. The AI is not allowed "
             "to invent prices, discounts or claims outside this.</p>")
    o.append(f"<div class=card>{'<br>'.join(e(l) for l in brief.splitlines())}</div>")

    # ---- 2 heard
    if asr:
        o.append("<h2><span class=step>2</span>What the AI heard in the footage</h2><p class=lead>Muse Voice Transcribe "
                 "turns speech into text with timing. Hindi and English mixed in one sentence is handled.</p><div class=grid>")
        for name, j in asr.items():
            o.append(f"<div class=card><h3>{e(name)}</h3><div class=said>{e(j.get('transcript', ''))}</div>"
                     f"<span class=mut>{j.get('audioDurationMs', 0) / 1000:.0f} seconds of speech</span></div>")
        o.append("</div>")

    # ---- 3 ideas
    o.append("<h2><span class=step>3</span>Three ideas, and the one a person picked</h2><p class=lead>Muse Spark wrote three "
             "different angles, each built only from moments that really exist in the footage. "
             "Nothing was built until a person chose one.</p><div class=grid>")
    for c in concepts:
        isp = c["id"] == picked
        o.append(f"<div class='card{' pick' if isp else ''}'>{'<span class=badge>Picked</span>' if isp else ''}"
                 f"<span class=pill>{e(c['angle'])} angle</span><h3>{e(c['title'])}</h3>"
                 f"<p><b>Opening line:</b> {e(c['hook'])}</p>"
                 f"<p class=mut><b>Why it should work:</b> {e(c['why_it_fits'])}</p>"
                 f"<p class=mut><b>Risk:</b> {e(c['risks'])}</p></div>")
    o.append("</div>")

    # ---- 4 preview
    if meta.get("frames"):
        o.append("<h2><span class=step>4</span>Preview before building</h2><p class=lead>Muse Image drew one frame per beat so "
                 "the idea can be judged cheaply. These are AI-generated stand-ins, not the real person.</p>"
                 "<div class=frames>" + "".join(f"<img src='{e(f)}' alt='storyboard frame'>" for f in meta["frames"]) + "</div>")

    # ---- 5 edit
    if final:
        o.append("<h2><span class=step>5</span>The edit</h2><p class=lead>The final cut is a sequence of moments from the "
                 "raw clips. Cuts land on pauses so no word is clipped, and the ad ends on a calm call-to-action card.</p><div class=card><ol>")
        for b in final["beats"]:
            o.append(f"<li><b>{e(b['clip'])}</b>, {b['src_in']:.1f}s to {b['src_out']:.1f}s: {e(b['purpose'])}"
                     f"{' <span class=mut>(on screen: “' + e(b['on_screen_text']) + '”)</span>' if b.get('on_screen_text') else ''}</li>")
        o.append(f"</ol><p><b>Call to action:</b> {e(final['cta'])}</p></div>")

    # ---- 6 final cuts
    if meta.get("cuts"):
        o.append("<h2><span class=step>6</span>The finished ad, three shapes</h2><p class=lead>Same ad, cut for each place it "
                 "can run. The square and wide versions keep the speaker in frame using SAM 3.1. Captions are burned in.</p><div class=videos>")
        for r, f in meta["cuts"].items():
            h = 440 if r == "9x16" else (340 if r == "1x1" else 240)
            o.append(f"<figure><video controls preload=metadata playsinline src='{e(f)}' height={h}></video>"
                     f"<figcaption>{e(RATIO.get(r, r))}</figcaption></figure>")
        o.append("</div>")

    # ---- 7 check
    if q:
        ok = q.get("pass")
        o.append("<h2><span class=step>7</span>Quality check by Muse Wonder</h2><p class=lead>A second AI reads the finished ad "
                 "against the brief. If it fails, the editing step is sent back with the reasons, and the ad is rebuilt.</p>")
        o.append(f"<div class='verdict{'' if ok else ' fail'}'><b class='{'ok' if ok else 'bad'}'>"
                 f"{'PASSED' if ok else 'NEEDS WORK'} · score {e(q.get('score'))} out of 5</b><p>{e(q.get('summary', ''))}</p>")
        if q.get("issues"):
            o.append("<p><b>Still flagged:</b></p><ul>" + "".join(f"<li>{e(i)}</li>" for i in q["issues"]) + "</ul>")
        o.append("</div>")
        if len(hist) > 1:
            o.append("<p class=mut><b>How it got there:</b> " + " → ".join(
                f"round {h['attempt']}: {e(h['score'])}/5" for h in hist) + "</p>")

    # ---- what we need from reviewer
    o.append("<h2>What we need from you</h2><div class=ask><p>If you work in ads, please answer honestly. "
             "This is the test that matters more than the AI's own score.</p><ol>"
             "<li>Would you show this to a client as it is? If not, what is the first thing you would change?</li>"
             "<li>Is the opening line strong enough to stop a scroll?</li>"
             "<li>Is there any line or claim you would not make for this brand?</li>"
             "<li>Are the captions right for how people actually write Hinglish?</li></ol></div>")

    # ---- decision log in sentences
    o.append("<h2>How every decision was made</h2><p class=lead>Each step the system took, in order, in plain words. "
             "Every call is recorded so a person can audit or override it.</p><div class=card><ol class=log>")
    for t in trace:
        stage, who = STAGE.get(t["tool"], ("Step", t["tool"]))
        flag = "" if t["status"] == "ok" else f" <span class=bad>({e(t['status'])})</span>"
        o.append(f"<li><span class=who>{e(stage)} · {e(who)}</span>{e(t.get('decision') or t['tool'])}{flag}</li>")
    o.append("</ol>")
    o.append("<details><summary>Under the hood: raw trace</summary><table><tr><th>#<th>Tool<th>ms<th>cost $<th>status<th>input hash<th>output hash</tr>")
    for t in trace:
        o.append(f"<tr><td>{t['step']}<td>{e(t['tool'])}<td>{t['duration_ms']}<td>{t.get('cost_usd', '')}"
                 f"<td>{e(t['status'])}<td>{e(t.get('input_hash', ''))}<td>{e(t.get('output_hash', ''))}</tr>")
    o.append("</table></details></div>")
    o.append(f"<p class=mut style='margin-top:36px'>Run {e(run_dir.name)} · built during the Meta Global AI Developer Hackathon · "
             "github.com/Vaibhavs18/adfarming</p></main></html>")

    p = run_dir / "index.html"
    p.write_text("".join(o))
    return p
