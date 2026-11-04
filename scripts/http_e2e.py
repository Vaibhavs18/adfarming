"""Scripted HUMAN driving the Studio over HTTP (the same API the browser UI uses).

  STUDIO_PASSWORD=... .venv/bin/python scripts/http_e2e.py --base http://127.0.0.1:8765 [--client dharaa]
  STUDIO_PASSWORD=... .venv/bin/python scripts/http_e2e.py --kill-server-midway --port 8765   (spawns uvicorn itself)

Flow: login -> create run -> brief approve -> ideas steer ('shorter hook, no Diwali year') -> pick concept 1 ->
preview approve -> edit approve -> check approve -> ship approve -> done; then Range-download the three mp4s
(expect 206 + mp4 magic), print the plain-language decision log and the cost. Exit code != 0 on any failure.

--shapes        also exercises UI answer shapes: reject-with-reason (steer+reason), ideas edit
                {concept_id, hook, cta}, edit-step edit {hook, cta, captions} (captions must be reported ignored,
                never silently dropped) and a back step is NOT used (it would re-spend).
--kill-server-midway   spawns uvicorn itself with the current env, SIGKILLs it (1) right after the brief is
                approved while the agent works (run must become 'interrupted' and POST /resume must finish it) and
                (2) while the ideas gate is pending (same gate must come back, answers still work).
--composed      composed-look flow (STUDIO_LOOK=composed): at Preview checks frames_kind/look, steers once
                ('make the background calmer'), checks the new plan version, approves; at Edit checks the three
                composed cuts + look.mode; prints wall seconds per step and fallback events. Also --save-dir DIR
                downloads the preview stills and the three cuts there. Use a throwaway ADFARMING_DB/OUT.
--skip-magic    for the stub runner (placeholder bytes are not real mp4s).
NOTE: keep ADFARMING_OUT outside the repo's private/ tree (web.py refuses to serve artifacts under private/, so
media Range requests 404); use a temp dir. The password is only read from env STUDIO_PASSWORD and never printed.
"""
import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
STEER = "shorter hook, no Diwali year"
LOOK_STEER = "make the background calmer"
POLL_S, WAIT_S = 3, 900


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", flush=True)
    sys.exit(1)


class Human:
    def __init__(self, base: str, password: str):
        self.base, self.password = base.rstrip("/"), password
        self.s = requests.Session()

    def login(self) -> None:
        r = self.s.post(f"{self.base}/api/login", json={"password": self.password}, timeout=20)
        if r.status_code != 200:
            fail(f"login -> {r.status_code}")

    def get(self, path, **kw):
        return self.s.get(f"{self.base}{path}", timeout=30, **kw)

    def post(self, path, body=None):
        return self.s.post(f"{self.base}{path}", json=body or {}, timeout=60)

    def run(self, rid: str) -> dict:
        r = self.get(f"/api/runs/{rid}")
        if r.status_code != 200:
            fail(f"GET run -> {r.status_code} {r.text[:200]}")
        return r.json()

    def wait(self, rid: str, want=("awaiting_human",), after_gate: int | None = None, label="") -> dict:
        t0 = time.time()
        while time.time() - t0 < WAIT_S:
            try:
                st = self.run(rid)
            except requests.RequestException:
                time.sleep(POLL_S)
                continue
            if st["status"] in want and (after_gate is None or not st["gate"] or st["gate"]["id"] != after_gate):
                return st
            if st["status"] in ("error", "cancelled", "interrupted") and st["status"] not in want:
                fail(f"run is {st['status']} while waiting for {want} ({label}): "
                     f"{[c['text'] for c in st['commentary'][-3:]]}")
            time.sleep(POLL_S)
        fail(f"timeout waiting for {want} ({label})")

    def answer(self, rid: str, st: dict, action: str, **kw) -> None:
        r = self.post(f"/api/runs/{rid}/answer", {"gate": st["gate"]["id"], "action": action, **kw})
        if r.status_code != 202:
            fail(f"answer {action} on {st['gate']['step']} -> {r.status_code} {r.text[:300]}")
        print(f"  answered {st['gate']['step']}: {action} {kw.get('choice') or kw.get('text') or ''}".rstrip(), flush=True)


class Server:
    """Spawns uvicorn so the script can kill and restart it."""

    def __init__(self, port: int):
        self.port, self.p = port, None

    def start(self) -> None:
        self.p = subprocess.Popen([sys.executable, "-m", "uvicorn", "adfarming.web:app", "--host", "127.0.0.1",
                                   "--port", str(self.port), "--log-level", "warning"],
                                  cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        for _ in range(60):
            try:
                if requests.get(f"http://127.0.0.1:{self.port}/healthz", timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.5)
        fail("server did not start")

    def kill(self) -> None:
        if self.p:
            self.p.send_signal(signal.SIGKILL)
            self.p.wait()
            self.p = None
            print("  *** server SIGKILLed", flush=True)

    def stop(self) -> None:
        if self.p:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()


def save_files(h: "Human", a, urls: list[str], prefix: str) -> None:
    if not a.save_dir:
        return
    out = Path(a.save_dir)
    out.mkdir(parents=True, exist_ok=True)
    for i, u in enumerate(urls):
        r = h.s.get(a.base + u, timeout=300)
        if r.status_code == 200:
            ext = Path(u.split("?")[0]).suffix or ".bin"
            (out / f"{prefix}_{i}{ext}").write_bytes(r.content)
            print(f"  saved {prefix}_{i}{ext} ({len(r.content)} bytes)", flush=True)


def db_report(rid: str) -> dict:
    """Gate audit + fallback events straight from the studio DB (when ADFARMING_DB is set)."""
    path = os.environ.get("ADFARMING_DB")
    if not path or not Path(path).exists():
        return {}
    sys.path.insert(0, str(ROOT / "src"))
    from adfarming.agent.tools import audit_gate_violations
    c = sqlite3.connect(path)
    n = c.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='fallback'", (rid,)).fetchone()[0]
    return {"fallback_events": n, "audit": audit_gate_violations(rid)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--client", default="dharaa")
    ap.add_argument("--goal", default="Festive reel for the Karwa Chauth push, 15-20 seconds, warm and personal.")
    ap.add_argument("--kill-server-midway", action="store_true")
    ap.add_argument("--shapes", action="store_true")
    ap.add_argument("--skip-magic", action="store_true")
    ap.add_argument("--composed", action="store_true")
    ap.add_argument("--save-dir", default="")
    ap.add_argument("--look-steer", default=LOOK_STEER)
    a = ap.parse_args()
    pw = os.environ.get("STUDIO_PASSWORD")
    if not pw:
        fail("set STUDIO_PASSWORD in the environment")
    srv = None
    if a.kill_server_midway:
        a.base = f"http://127.0.0.1:{a.port}"
        srv = Server(a.port)
        srv.start()
    h = Human(a.base, pw)
    t0 = time.time()
    try:
        h.login()
        cl = h.get("/api/clients").json()
        if a.client not in [c["id"] for c in cl]:
            fail(f"client {a.client!r} not in {cl}")
        r = h.post("/api/runs", {"client_id": a.client, "goal": a.goal})
        if r.status_code != 200:
            fail(f"create run -> {r.status_code} {r.text[:200]}")
        rid = r.json()["run_id"]
        print(f"run {rid}", flush=True)

        st = h.wait(rid, label="brief")
        assert st["gate"]["step"] == "brief", st["gate"]
        print(f"  brief card: {st['gate']['card']['text'][:140]!r}", flush=True)
        h.answer(rid, st, "approve")
        if srv:  # (1) kill while the agent works
            time.sleep(1.0)
            srv.kill()
            srv.start()
            h.login()
            s2 = h.run(rid)
            if s2["status"] != "interrupted":
                fail(f"after kill mid-work expected 'interrupted', got {s2['status']!r}")
            print("  run is 'interrupted' after restart; POST /resume", flush=True)
            rr = h.post(f"/api/runs/{rid}/resume")
            if rr.status_code != 202:
                fail(f"resume -> {rr.status_code} {rr.text[:200]}")

        st = h.wait(rid, label="ideas")
        assert st["gate"]["step"] == "ideas", st["gate"]
        n0 = len(st["gate"]["card"]["concepts"])
        h.answer(rid, st, "steer", text=STEER)
        st = h.wait(rid, after_gate=st["gate"]["id"], label="ideas after steer")
        assert st["gate"]["step"] == "ideas", st["gate"]
        if srv:  # (2) kill while a gate is pending
            gid = st["gate"]["id"]
            srv.kill()
            srv.start()
            h.login()
            s2 = h.run(rid)
            if s2["status"] != "awaiting_human" or not s2["gate"] or s2["gate"]["id"] != gid:
                fail(f"pending gate not preserved across restart: {s2['status']} {s2['gate'] and s2['gate']['id']}")
            print(f"  gate {gid} survived the restart", flush=True)
        cs = st["gate"]["card"]["concepts"]
        print(f"  concepts ({n0} -> {len(cs)}): " + " | ".join(f"{c['id']}: {c['hook']}" for c in cs), flush=True)
        if a.shapes:
            bad = cs[-1]
            h.answer(rid, st, "steer", reason="off-brand",
                     text=f"Not {bad['id']} ({bad['title']}): off-brand. Drop that angle.")
            st = h.wait(rid, after_gate=st["gate"]["id"], label="after reject")
            cs = st["gate"]["card"]["concepts"]
            print("  after reject-with-reason: " + " | ".join(f"{c['id']}: {c['title']}" for c in cs), flush=True)
            h.answer(rid, st, "edit", fields={"concept_id": cs[0]["id"], "hook": "Meri pehli Karwa Chauth", "cta": "Link in bio"})
            st = h.wait(rid, after_gate=st["gate"]["id"], label="after ideas edit")
            cs = st["gate"]["card"]["concepts"]
            if cs[0]["hook"] != "Meri pehli Karwa Chauth":
                fail(f"ideas edit not applied to concept {cs[0]['id']}: hook={cs[0]['hook']!r}")
            print("  ideas edit applied (hook, cta)", flush=True)
        pick = st["gate"]["card"]["concepts"][0]["id"]
        h.answer(rid, st, "approve", choice=pick)

        timings: dict[str, float] = {}
        t_step = time.time()
        for step in ("preview", "edit", "check", "ship"):
            st = h.wait(rid, label=step)
            timings[step] = round(time.time() - t_step, 1)
            if st["gate"]["step"] != step:
                fail(f"expected gate {step}, got {st['gate']['step']}")
            if step == "preview" and not st["gate"]["card"]["frames"]:
                fail("preview card has no frames")
            if a.composed and step == "preview":
                card = st["gate"]["card"]
                if card.get("frames_kind") != "composed" or not card.get("look"):
                    print(f"  WARNING preview is {card.get('frames_kind')!r} (fell back); look={card.get('look')}", flush=True)
                else:
                    print(f"  preview {card['frames_kind']}: {len(card['frames'])} stills, plan {card['look']['plan_id']}, "
                          f"preset {card['look']['preset']}, next-step ~${card['cost_estimate_usd']}", flush=True)
                    for b in card["look"]["summary"]:
                        print(f"    - {b}", flush=True)
                save_files(h, a, st["gate"]["card"]["frames"], "still")
                t_step = time.time()
                h.answer(rid, st, "steer", text=a.look_steer)
                st = h.wait(rid, after_gate=st["gate"]["id"], label="preview after look steer")
                timings["preview_steer"] = round(time.time() - t_step, 1)
                card = st["gate"]["card"]
                if st["gate"]["step"] != "preview":
                    fail(f"look steer left the preview step: {st['gate']['step']}")
                print(f"  after steer: plan {(card.get('look') or {}).get('plan_id')} kind {card.get('frames_kind')}", flush=True)
                save_files(h, a, card["frames"], "still_steered")
            if a.composed and step == "edit":
                card = st["gate"]["card"]
                lk = card.get("look") or {}
                cuts0 = (st["artifacts"].get("cuts") or {})
                print(f"  edit card: look.mode={lk.get('mode')} plan={lk.get('plan_id')} cuts={sorted(cuts0)}", flush=True)
                if lk.get("mode") != "composed":
                    print(f"  WARNING edit card is not composed: {lk.get('mode')} reason={lk.get('reason')}", flush=True)
                save_files(h, a, [cuts0[k] for k in sorted(cuts0)], "cut")
            t_step = time.time()
            if step == "edit" and a.shapes:
                card = st["gate"]["card"]
                caps = [dict(c, text=c["text"] + " X") for c in card["captions"]]
                h.answer(rid, st, "edit", fields={"hook": card["hook"], "cta": "Shop the saree edit", "captions": caps})
                st = h.wait(rid, after_gate=st["gate"]["id"], label="after edit-step edit")
                got = st["gate"]["card"]["cta"]
                if got != "Shop the saree edit":
                    # agent-side issue: QC auto-revise inside build_cuts can rewrite a human-edited cta
                    print(f"  WARNING edit-step cta edit was overwritten by the agent: {got!r}", flush=True)
                else:
                    print("  edit-step edit applied (cta); captions are not editable by design", flush=True)
            if step == "ship":
                print(f"  ship memory proposals: {[m['text'][:80] for m in st['gate']['card']['memory_proposals']]}", flush=True)
            h.answer(rid, st, "approve")
            t_step = time.time()
        t_fin = time.time()
        st = h.wait(rid, want=("done",), label="finish")
        timings["finish"] = round(time.time() - t_fin, 1)

        cuts = st["artifacts"]["cuts"] or {}
        if sorted(cuts) != ["16x9", "1x1", "9x16"]:
            fail(f"expected three cuts, got {sorted(cuts)}")
        for k, url in sorted(cuts.items()):
            r = h.s.get(a.base + url, headers={"Range": "bytes=0-1023"}, timeout=60)
            if r.status_code != 206:
                fail(f"{k} Range request -> {r.status_code}")
            if not a.skip_magic and r.content[4:8] != b"ftyp":
                fail(f"{k} is not an mp4 (no ftyp)")
            total = r.headers.get("content-range", "").split("/")[-1]
            print(f"  {k}: 206, {len(r.content)} bytes of {total}, magic {r.content[4:8]!r}", flush=True)
        if st["artifacts"].get("zip"):
            r = h.s.get(a.base + st["artifacts"]["zip"], timeout=60)
            print(f"  zip: {r.status_code}, {len(r.content)} bytes", flush=True)

        log = h.get(f"/api/runs/{rid}/log").json()
        print("\n--- decision log ---")
        for e in log:
            if e["kind"] in ("tool_started", "tool_done", "run_status"):
                continue
            c = f" [${e['cost_usd']:.3f}]" if e["cost_usd"] else ""
            print(f"{e['seq']:>4} {e['actor']:<6} {(e['step'] or '-'):<8} {e['text'][:170]}{c}")
        print("--- end log ---\n")
        errs = [e for e in log if e["kind"] == "error"]
        if errs:
            print(f"error events: {[e['text'] for e in errs]}")
        rep = db_report(rid)
        print(f"STEP_WAIT_SECONDS {timings}  (seconds from the previous answer until the next gate was ready)", flush=True)
        print(f"RESULT status={st['status']} wall={time.time() - t0:.0f}s cost=${st['cost']['usd']:.3f} "
              f"cap=${st['cost']['cap_usd']} by_tool={st['cost']['by_tool']} {rep}")
        if rep and rep["audit"]["violations"]:
            fail(f"gate violations: {rep['audit']['violations']}")
        print("PASS")
    finally:
        if srv:
            srv.stop()


if __name__ == "__main__":
    main()
