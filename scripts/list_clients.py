#!/usr/bin/env python3
"""List the clients served from ADF_PRIVATE (clients/*/client.json, plus the flat default).
  python scripts/list_clients.py [--private DIR]"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from adfarming import clients  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--private", help="ADF_PRIVATE override")
a = ap.parse_args()
if a.private:
    os.environ["ADF_PRIVATE"] = a.private
rows = clients.list_clients()
print(f"ADF_PRIVATE={clients.private_root()}  layout={'clients/' if clients.layout_enabled() else 'flat'}")
if not rows:
    print("(no clients)")
for c in rows:
    d = c.dir
    foot = [f for f in (d / "footage").glob("*") if f.suffix.lower() in clients.CLIP_EXT] if (d / "footage").is_dir() else []
    asr = len(list((d / "asr").glob("*.json"))) if (d / "asr").is_dir() else 0
    seed = len(clients.seed_items(c.id)) if (d / "seed_memory.jsonl").exists() or (d / "rules.md").exists() else 0
    print(f"{c.id:<16} {c.name:<28} clips={len(foot):<3} asr={asr:<3} seed_rules={seed:<3} "
          f"brief={'yes' if (d / 'brief.md').exists() else 'NO'} {'(flat)' if c.flat else ''}")
