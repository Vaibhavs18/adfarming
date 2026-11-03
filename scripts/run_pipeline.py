"""Usage:
  python scripts/run_pipeline.py RUN_ID            # concepts only, then stop at the human gate
  python scripts/run_pipeline.py RUN_ID --pick c2  # human picked c2: storyboard + cuts + review page
"""
import argparse
import json
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from adfarming import concepts as C, cuts, qc, sam, storyboard, viewer  # noqa: E402
from adfarming.footage import PRIV, load_clips  # noqa: E402
from adfarming.trace import Run  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_id")
ap.add_argument("--pick")
ap.add_argument("--no-storyboard", action="store_true")
a = ap.parse_args()

run = Run(a.run_id)
clips = load_clips()
cfile = run.dir / "concepts.json"
if cfile.exists():
    print("reusing concepts.json")
    run.step = len(run.dir.joinpath("trace.jsonl").read_text().splitlines()) if (run.dir / "trace.jsonl").exists() else 0
else:
    refs = (PRIV / "refs.md").read_text() if (PRIV / "refs.md").exists() else ""
    cs = C.generate(run, (PRIV / "brief.md").read_text(), clips, refs)
    cfile.write_text(json.dumps(cs, ensure_ascii=False, indent=2))
cs = json.loads(cfile.read_text())
meta = {}
if a.pick:
    concept = next(c for c in cs if c["id"] == a.pick)
    run.trace("human-approval", {"options": [c["id"] for c in cs]}, {"picked": a.pick}, started=time.time(),
              decision=f"human approved concept {a.pick} before any paid generation or cuts")
    brand = (PRIV / "subject.txt").read_text().strip() if (PRIV / "subject.txt").exists() else "an Indian woman in a saree"
    if not a.no_storyboard:
        meta["frames"] = storyboard.frames(run, concept, brand)
    brief = (PRIV / "brief.md").read_text()
    history = []
    for attempt in range(1, 4):
        meta.update(cuts.build(run, concept, clips, track=lambda m: sam.track(run, Path(m))))
        verdict = qc.review(run, brief, concept, meta["captions"], run.dir / meta["cuts"]["9x16"])
        history.append({"attempt": attempt, "score": verdict.get("score"), "pass": verdict.get("pass"),
                        "summary": verdict.get("summary")})
        meta["qc"], meta["qc_history"] = verdict, history
        if verdict.get("pass") or attempt == 3:
            break
        concept = C.revise(run, brief, concept, verdict, clips)
        cs = [concept if c["id"] == concept["id"] else c for c in cs]
        cfile.write_text(json.dumps(cs, ensure_ascii=False, indent=2))
    (run.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
page = viewer.render(run.dir, a.pick, meta)
print(f"\nreview page: {page}")
for c in cs:
    print(f"[{c['id']}] {c['title']} ({c['angle']}, {c['length_s']}s) hook: {c['hook']}")
