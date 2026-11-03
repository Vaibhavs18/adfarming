"""Scripted fake Studio session against a temp DB; prints the event log."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["ADFARMING_DB"] = str(Path(tempfile.mkdtemp()) / "smoke.db")

from adfarming.studio import events, gates, memory  # noqa: E402

seed = ROOT / "private/research/seed_memory_dharaa"
if seed.exists():
    print("seeded", memory.load_seed("dharaa", seed), "items")
else:
    memory.add_human_memory("dharaa", "hard_rule", "No hard-sell, no fake urgency", pinned=True)
mem = memory.read_memory("dharaa", 1200)
run = events.create_run("dharaa", brief="Karwa Chauth push")
memory.read_memory("dharaa", 1200, run_id=run)


def try_tool(tool):
    try:
        gates.assert_approved(run, tool)
        print(f"  [{tool}] allowed")
    except gates.GateBlocked as e:
        print(f"  [{tool}] BLOCKED: {e}")


g = gates.request_gate(run, "brief", {"restated": "..."}); gates.answer_gate(g, "approve")
gates.propose_artifact(run, "ideas", "concepts")
g = gates.request_gate(run, "ideas"); try_tool("storyboard")
out = gates.answer_gate(g, "steer", text="c2 but shorter hook, and no Diwali")
print("steer -> memory", out["memory_item"]); try_tool("storyboard")
gates.propose_artifact(run, "ideas", "concepts")
g = gates.request_gate(run, "ideas"); gates.answer_gate(g, "approve"); try_tool("storyboard")
events.record_cost(run, "storyboard", 0.04)
gates.propose_artifact(run, "preview", "frames")
g = gates.request_gate(run, "preview", {"cost_estimate": 0.3}); try_tool("build_cuts")
gates.answer_gate(g, "edit", {"fields": {"cta": "See the saree on our website"}, "artifact": "frames"})
gates.propose_artifact(run, "preview", "frames")
g = gates.request_gate(run, "preview"); gates.answer_gate(g, "approve"); try_tool("build_cuts")
events.record_cost(run, "build_cuts", 0.2)
gates.propose_artifact(run, "edit", "cuts")
g = gates.request_gate(run, "edit"); gates.answer_gate(g, "back", {"to": "ideas"}, reason="hook too long")
try_tool("build_cuts")
memory.decide_memory(out["memory_item"]["item_id"], "accept", run_id=run)
print("memory injected next run:", [m["text"] for m in memory.read_memory("dharaa", 1200)][-1])
print("\nEVENT LOG")
for e in events.list_events(run):
    print(f"{e['id']:>3} {e['actor']:<6} {e['kind']:<18} {e['payload']} {e['reason'] or ''}")
st = events.rebuild_run_state(run)
print("\nSTATE", {k: st[k] for k in ("current_step", "pending_gate", "cost_usd", "stale_artifacts", "step_approval")})
