"""Static review page: concepts, storyboard frames, cuts, and the full decision trace."""
import html
import json
from pathlib import Path

CSS = """:root{--bg:#fff;--fg:#14161a;--mut:#667085;--card:#f5f6f8;--line:#e3e5e9;--acc:#0a66ff}
@media(prefers-color-scheme:dark){:root{--bg:#101215;--fg:#eceef2;--mut:#98a2b3;--card:#1a1d22;--line:#2b2f36;--acc:#6aa1ff}}
body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{margin:0 0 4px}h2{margin:32px 0 12px}
.mut{color:var(--mut)}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card.pick{border-color:var(--acc);box-shadow:0 0 0 2px var(--acc) inset}
.tag{display:inline-block;font-size:12px;border:1px solid var(--line);border-radius:99px;padding:1px 9px;margin-right:6px}
img{max-width:100%;border-radius:8px}video{max-width:100%;border-radius:8px;background:#000}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}"""


def e(x) -> str:
    return html.escape(str(x))


def render(run_dir: Path, picked: str | None, meta: dict) -> Path:
    concepts = json.loads((run_dir / "concepts.json").read_text())
    trace = [json.loads(l) for l in (run_dir / "trace.jsonl").read_text().splitlines()]
    o = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>AdFarming run {e(run_dir.name)}</title><style>{CSS}</style><main>",
         f"<h1>AdFarming</h1><p class=mut>Run {e(run_dir.name)} · {len(trace)} traced steps · "
         f"human picked: <b>{e(picked or 'not yet')}</b></p><h2>Concepts</h2><div class=grid>"]
    for c in concepts:
        beats = "".join(f"<li>{e(b['clip'])} {b['src_in']}-{b['src_out']}s · {e(b['purpose'])}"
                        f"{' · <i>' + e(b['on_screen_text']) + '</i>' if b.get('on_screen_text') else ''}</li>"
                        for b in c["beats"])
        o.append(f"<div class='card{' pick' if c['id'] == picked else ''}'><span class=tag>{e(c['angle'])}</span>"
                 f"<span class=tag>{e(c['length_s'])}s</span><h3>{e(c['title'])}</h3><p><b>Hook:</b> {e(c['hook'])}</p>"
                 f"<ol>{beats}</ol><p><b>CTA:</b> {e(c['cta'])}</p><p class=mut><b>Why:</b> {e(c['why_it_fits'])}</p>"
                 f"<p class=mut><b>Risk:</b> {e(c['risks'])}</p></div>")
    o.append("</div>")
    if meta.get("frames"):
        o.append("<h2>Storyboard preview (Muse Image)</h2><div class=row>" +
                 "".join(f"<img src='{e(f)}' width=200>" for f in meta["frames"]) + "</div>")
    if meta.get("cuts"):
        o.append("<h2>Final cuts</h2><div class=row>" + "".join(
            f"<div><div class=mut>{e(r)}</div><video controls preload=metadata src='{e(f)}' "
            f"height={320 if r != '16x9' else 220}></video></div>" for r, f in meta["cuts"].items()) + "</div>")
    o.append("<h2>Decision trace</h2><table><tr><th>#<th>Tool<th>Decision<th>ms<th>$<th>Status</tr>")
    for t in trace:
        o.append(f"<tr><td>{t['step']}<td>{e(t['tool'])}<td>{e(t.get('decision', ''))}<td>{t['duration_ms']}"
                 f"<td>{t.get('cost_usd', '')}<td>{e(t['status'])}</tr>")
    o.append("</table></main>")
    p = run_dir / "index.html"
    p.write_text("".join(o))
    return p
