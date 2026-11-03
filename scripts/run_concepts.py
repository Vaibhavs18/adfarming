"""Usage: python scripts/run_concepts.py [run_id]. Reads private/brief.md + private/transcripts.json."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from adfarming.concepts import generate  # noqa: E402
from adfarming.trace import Run  # noqa: E402

root = Path(__file__).resolve().parents[1]
priv = root / "private"
brief = (priv / "brief.md").read_text()
refs = (priv / "refs.md").read_text() if (priv / "refs.md").exists() else ""
tr = json.loads((priv / "transcripts.json").read_text())

clips = []
for f in sorted((priv / "footage").iterdir()):
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)]))
    clips.append({"file": f.name, "duration_s": round(dur, 1), "transcript": tr[f.stem]["text"]})

run = Run(sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("run-%Y%m%d-%H%M%S"))
concepts = generate(run, brief, clips, refs)
(run.dir / "concepts.json").write_text(json.dumps(concepts, ensure_ascii=False, indent=2))
print(f"run {run.run_id}: {len(concepts)} concepts -> {run.dir}")
for c in concepts:
    print(f"\n[{c['id']}] {c['title']} ({c['angle']}, {c['length_s']}s)\n  hook: {c['hook']}\n  cta: {c['cta']}\n  why: {c['why_it_fits']}")
