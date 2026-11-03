"""REAL end-to-end session on local client footage with a scripted human (local only: private/ is never copied).

  .venv/bin/python scripts/agent_e2e.py --driver scripted|wonder [--process-per-advance] [--tag NAME]

Uses a fresh temp DB per run. With --process-per-advance every advance() runs in a NEW Python process that
exits (os._exit) right after the gate is created, i.e. the process is killed between ask_human and the
answer, then restarted: all state comes from the event log.
Scripted human: approve brief; steer once on ideas ('shorter hook, no Diwali year'); then pick the FIRST
concept; approve preview, edit, check, ship. Writes a JSON report to out/e2e_reports/ (gitignored).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

STEER = "shorter hook, no Diwali year"
GOAL = "Festive reel for the Karwa Chauth push, 15-20 seconds, warm and personal."
DIWALI_YEAR = re.compile(r"diwali[^.\n]{0,25}?(\b(19|20)\d\d\b|twenty|eighteen|nineteen)|(\b(19|20)\d\d\b)[^.\n]{0,15}diwali",
                         re.I)


def child_advance(run_id: str) -> None:
    from adfarming.agent import AgentLoop
    res = AgentLoop().advance(run_id)
    print("RESULT " + json.dumps(res), flush=True)
    os._exit(0)  # hard exit: nothing survives except the DB and out/


def advance(run_id: str, per_process: bool, env: dict) -> dict:
    if not per_process:
        from adfarming.agent import AgentLoop
        return AgentLoop().advance(run_id)
    p = subprocess.run([sys.executable, __file__, "--child-advance", run_id], env=env, capture_output=True,
                       text=True, cwd=ROOT)
    line = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")]
    if not line:
        raise RuntimeError(f"child died: rc={p.returncode} stderr={p.stderr[-400:]}")
    return json.loads(line[-1][7:])


def hooks_text(c: dict) -> str:
    return " | ".join([c.get("hook") or "", c.get("cta") or "", *[b.get("on_screen_text") or "" for b in c.get("beats", [])]])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", default="wonder", choices=["wonder", "scripted"])
    ap.add_argument("--process-per-advance", action="store_true")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--client", default="dharaa")
    ap.add_argument("--child-advance")
    a = ap.parse_args()
    if a.child_advance:
        return child_advance(a.child_advance)

    tag = a.tag or f"{a.driver}-{int(time.time())}"
    tmp = Path(tempfile.mkdtemp(prefix="adf_e2e_"))
    os.environ["ADFARMING_DB"] = str(tmp / "studio.db")
    os.environ.setdefault("ADFARMING_OUT", str(ROOT / "out" / "e2e" ))
    os.environ["STUDIO_AGENT"] = a.driver
    from adfarming.agent import audit_gate_violations
    from adfarming.studio import events, gates, memory

    seed = ROOT / "private/research/seed_memory_dharaa"
    print("seeded", memory.load_seed(a.client, seed), "memory items")
    run_id = events.create_run(a.client, brief=GOAL)
    env = dict(os.environ)
    t0 = time.time()
    log, steered, pauses = [], False, 0
    first_choice = None
    results = []
    for _ in range(40):
        r = advance(run_id, a.process_per_advance, env)
        results.append(r)
        print(f"[{time.time() - t0:6.0f}s] advance -> {r['status']} step={r.get('step')} cost=${r['cost_usd']:.3f}", flush=True)
        if r["status"] != "awaiting_human":
            break
        pauses += 1
        gate = events.rebuild_run_state(run_id)["pending_gate"]
        step, card = gate["step"], gate["payload"]["card"]
        if step == "ideas" and not steered:
            steered = True
            gates.answer_gate(gate["gate_id"], "steer", text=STEER)
            log.append(("ideas", "steer"))
        elif step == "ideas":
            first_choice = card["concepts"][0]["id"]
            gates.answer_gate(gate["gate_id"], "approve", {"choice": first_choice})
            log.append(("ideas", f"approve {first_choice}"))
        else:
            gates.answer_gate(gate["gate_id"], "approve")
            log.append((step, "approve"))
    wall = time.time() - t0

    evs = events.list_events(run_id)
    st = events.rebuild_run_state(run_id)
    out = ROOT / "out" / "e2e" if False else Path(os.environ["ADFARMING_OUT"]) / run_id
    versions = sorted(out.glob("concepts.v*.json"), key=lambda p: int(re.search(r"v(\d+)", p.name).group(1)))
    v1 = json.loads(versions[0].read_text()) if versions else []
    vN = json.loads(versions[-1].read_text()) if versions else []
    wc = json.loads((out / "working_concept.json").read_text())["concept"] if (out / "working_concept.json").exists() else {}
    avg = lambda cs: sum(len((c.get("hook") or "").split()) for c in cs) / max(1, len(cs))
    steer_tool = next((e["payload"]["tool"] for e in evs if e["kind"] == "tool_call" and e["payload"]["tool"] in
                       ("write_concepts", "revise_concept") and e["id"] > next((x["id"] for x in evs if x["kind"] == "steer"), 1e9)), None)
    steer_ev = next((x["id"] for x in evs if x["kind"] == "steer"), None)
    after_hooks = [c.get("hook") for c in vN]
    steer_applied = bool(steer_tool) and [c.get("hook") for c in v1] != after_hooks \
        and not any(DIWALI_YEAR.search(hooks_text(c)) for c in vN)
    turns = [dict(e["payload"], id=e["id"]) for e in evs if e["kind"] == "step_turn"]
    report = {
        "tag": tag, "driver": a.driver, "process_per_advance": a.process_per_advance, "run_id": run_id,
        "final_status": results[-1]["status"], "wall_s": round(wall, 1), "cost_usd": st["cost_usd"],
        "cost_by_tool": st["cost_by_tool"], "pauses": pauses,
        "gate_audit": audit_gate_violations(run_id),
        "fallbacks": [e["payload"] for e in evs if e["kind"] == "fallback"],
        "chain_fallbacks": sum(1 for e in evs if e["kind"] == "chain_fallback"),
        "step_turns": [{k: t[k] for k in ("step", "driver", "status", "turns", "calls", "tools", "usd", "secs", "error")} for t in turns],
        "wonder_turns_total": sum(t["turns"] for t in turns),
        "tool_errors": [(e["payload"]["tool"], e["payload"].get("error")) for e in evs
                        if e["kind"] == "tool_done" and not e["payload"]["ok"]],
        "steer": {"text": STEER, "tool_after_steer": steer_tool, "versions": len(versions),
                  "hooks_before": [c.get("hook") for c in v1], "hooks_after": after_hooks,
                  "avg_hook_words_before": round(avg(v1), 1), "avg_hook_words_after": round(avg(vN), 1),
                  "diwali_year_before": sum(bool(DIWALI_YEAR.search(hooks_text(c))) for c in v1),
                  "diwali_year_after": sum(bool(DIWALI_YEAR.search(hooks_text(c))) for c in vN),
                  "guidance_contains_steer": any(STEER in (e["payload"].get("args") or {}).get("guidance", "")
                                                 or STEER in (e["payload"].get("args") or {}).get("steer", "")
                                                 for e in evs if e["kind"] == "tool_call"),
                  "applied": steer_applied},
        "picked": first_choice, "final_hook": wc.get("hook"),
        "memory_proposed": [m["text"] for m in memory.list_memory(a.client, "proposed")],
        "commentary_events": sum(1 for e in evs if e["kind"] == "commentary"),
        "videos": sorted(p.name for p in out.glob("*.mp4")),
    }
    rep_dir = ROOT / "out" / "e2e_reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"{tag}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("tag", "final_status", "wall_s", "cost_usd", "pauses", "gate_audit",
                                              "fallbacks", "wonder_turns_total", "tool_errors")}, ensure_ascii=False))
    print("steer:", json.dumps(report["steer"], ensure_ascii=False))
    for t in report["step_turns"]:
        print("  turn", t)


if __name__ == "__main__":
    main()
