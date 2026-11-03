"""Usage:
  python scripts/run_pipeline.py RUN_ID            # concepts only, then stop at the human gate
  python scripts/run_pipeline.py RUN_ID --pick c2  # human picked c2: storyboard + cuts + review page
"""
import argparse
import copy
import json
import shutil
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
    history, rounds = [], []   # rounds: per-round snapshot so the BEST round is delivered, not the last

    def safe_track(m):
        try:
            return sam.track(run, Path(m))
        except Exception as e:  # SAM failure -> fixed crop
            run.trace("sam-3.1", {}, {"error": str(e)[:100]}, status="error", decision="SAM failed, fixed crop")
            return None

    for attempt in range(1, 4):
        rmeta = copy.deepcopy(cuts.build(run, concept, clips, track=safe_track))
        verdict = qc.review(run, brief, concept, rmeta["captions"], run.dir / rmeta["cuts"]["9x16"])
        rdir = run.dir / f"round{attempt}"
        rdir.mkdir(exist_ok=True)
        for f in [*rmeta["cuts"].values(), "qc_0.jpg", "qc_1.jpg", "qc_2.jpg", "qc_3.jpg"]:
            if (run.dir / f).exists():
                shutil.copy2(run.dir / f, rdir / f)
        shutil.copy2(rmeta["master"], rdir / "master_hold.mp4")
        score = verdict.get("score") if isinstance(verdict.get("score"), (int, float)) else None
        history.append({"attempt": attempt, "score": score, "pass": verdict.get("pass") is True,
                        "summary": verdict.get("summary")})
        rounds.append({"meta": rmeta, "verdict": verdict, "concept": copy.deepcopy(concept), "score": score})
        if verdict.get("pass") is True or score is None or attempt == 3:
            break  # passed, QC unavailable (nothing to revise from), or out of rounds
        prev = rounds[-2]["score"] if len(rounds) > 1 else None
        if prev is not None and score <= prev:
            break  # no improvement over the previous round: stop spending
        concept = C.revise(run, brief, concept, verdict, clips)
    bi = max(range(len(rounds)), key=lambda i: (rounds[i]["verdict"].get("pass") is True,
                                                rounds[i]["score"] if rounds[i]["score"] is not None else -1))
    best = rounds[bi]
    for f in best["meta"]["cuts"].values():  # best round's files become the final names
        shutil.copy2(run.dir / f"round{bi + 1}" / f, run.dir / f)
    for i in range(4):
        if (run.dir / f"round{bi + 1}" / f"qc_{i}.jpg").exists():
            shutil.copy2(run.dir / f"round{bi + 1}" / f"qc_{i}.jpg", run.dir / f"qc_{i}.jpg")
    meta.update(best["meta"])
    meta["qc"], meta["qc_history"], meta["best_round"] = best["verdict"], history, bi + 1
    cs = [best["concept"] if c["id"] == best["concept"]["id"] else c for c in cs]
    cfile.write_text(json.dumps(cs, ensure_ascii=False, indent=2))
    run.trace("deliver-best-round", {"scores": [h["score"] for h in history]}, {"best_round": bi + 1}, status="ok",
              decision=f"delivered round {bi + 1} (score {best['score']}) of {len(rounds)}; best-scoring round, not the last")
    (run.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
page = viewer.render(run.dir, a.pick, meta)
print(f"\nreview page: {page}")
for c in cs:
    print(f"[{c['id']}] {c['title']} ({c['angle']}, {c['length_s']}s) hook: {c['hook']}")
