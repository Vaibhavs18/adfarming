#!/usr/bin/env python3
"""Create (or refresh with --force) a client under ADF_PRIVATE/clients/<id>/ from a folder a human collected.

  python scripts/add_client.py --id acme --name 'Acme Tea' --from ~/Downloads/acme_pack [--no-asr] [--force]

<dir> may hold, anywhere inside it (searched recursively):
  *.mp4 / *.mov / *.m4v        loose clips -> footage/ (copied as-is, nothing is transcoded)
  brief.md|brief.txt|brief.docx   the brief            -> brief.md   (asked on stdin if missing)
  subject.txt                     who is on camera      -> subject.txt (asked on stdin if missing)
  rules.*|dos_and_donts.*         one rule per line, 'DO:' / "DON'T:" or '+' / '-' prefix
                                  -> seed_memory.jsonl (hard_rule, source=human, status=approved)
  refs.*|notes.*|past*            past-ad notes -> refs.md
ASR (Meta Voice Transcribe, about $0.003 per clip) is pre-computed per clip unless --no-asr.
Private client data stays under ADF_PRIVATE (local only; ship it with deploy/scripts/push_client_data.sh).
"""
import argparse
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from adfarming import clients  # noqa: E402

ASR_USD_PER_CLIP = 0.003
BRIEF_PLACEHOLDER = """# Brief for {name}

(Replace everything below. The agent reads this at every step.)

Client: {name} - what the brand sells, who buys it.
Goal: what the ad must achieve (e.g. website visits that convert).
Platform: e.g. Instagram/Facebook Reels and Stories, 9:16 first.
Tone: how the brand sounds (language mix, warmth, formality).
Must say / must avoid: claims the client can back, claims it cannot.
"""
RULE_NAMES = re.compile(r"^(rules?|dos?[_ -]?and[_ -]?don'?ts?|do[_ -]?dont|dos_donts)", re.I)
REF_NAMES = re.compile(r"^(refs?|references?|notes?|past)", re.I)


def docx_text(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def read_text(p: Path) -> str:
    return docx_text(p) if p.suffix.lower() == ".docx" else p.read_text(errors="replace")


def ask(question: str, placeholder: str, interactive: bool) -> str:
    """Read from stdin; a blank answer (or no stdin) keeps the clearly-marked placeholder."""
    if not interactive:
        return placeholder
    print(f"\n{question}\n(type the text and finish with a blank line; just press Enter to keep the placeholder)")
    lines = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines) if lines else placeholder


def scan(src: Path) -> dict:
    found = {"clips": [], "brief": None, "subject": None, "rules": [], "refs": []}
    for p in sorted(src.rglob("*")):
        if not p.is_file() or any(part.startswith((".", "__MACOSX")) for part in p.relative_to(src).parts):
            continue
        ext, stem = p.suffix.lower(), p.stem
        if ext in clients.CLIP_EXT:
            found["clips"].append(p)
        elif ext in (".md", ".txt", ".docx"):
            if stem.lower() == "brief" and found["brief"] is None:
                found["brief"] = p
            elif stem.lower() == "subject" and ext != ".docx":
                found["subject"] = p
            elif RULE_NAMES.match(stem):
                found["rules"].append(p)
            elif REF_NAMES.match(stem):
                found["refs"].append(p)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="lowercase letters/digits/-/_ (becomes the folder name)")
    ap.add_argument("--name", required=True, help="display name shown in the Studio picker")
    ap.add_argument("--from", dest="src", required=True, help="folder the human collected")
    ap.add_argument("--subject", help="one line describing who is on camera (else subject.txt or prompt)")
    ap.add_argument("--language", default="", help="language hint, e.g. 'Hinglish'")
    ap.add_argument("--no-asr", action="store_true", help="skip Meta Voice Transcribe (no API use)")
    ap.add_argument("--force", action="store_true", help="refresh an existing client (never deletes files)")
    ap.add_argument("--non-interactive", action="store_true", help="never prompt; use placeholders")
    ap.add_argument("--private", help="ADF_PRIVATE override (default: env or <repo>/private)")
    a = ap.parse_args()

    if not clients.CLIENT_ID_RE.match(a.id):
        sys.exit(f"bad --id {a.id!r}: use lowercase letters, digits, '-' or '_' (max 40)")
    src = Path(a.src).expanduser().resolve()
    if not src.is_dir():
        sys.exit(f"--from is not a directory: {src}")
    if a.private:
        import os
        os.environ["ADF_PRIVATE"] = a.private
    dest = clients.private_root() / "clients" / a.id
    if dest.exists() and not a.force:
        sys.exit(f"client {a.id!r} already exists at {dest}. Use --force to refresh it (files are never deleted).")
    if src == dest or dest in src.parents:
        sys.exit("--from must not be the client's own directory")

    found = scan(src)
    if not found["clips"]:
        sys.exit(f"no .mp4/.mov/.m4v clips found under {src}")
    interactive = sys.stdin.isatty() and not a.non_interactive
    todo = [c for c in found["clips"] if not (dest / "asr" / f"{c.stem}.json").exists()]
    cost = len(todo) * ASR_USD_PER_CLIP
    print(f"Found {len(found['clips'])} clip(s) in {src}")
    print("ASR: skipped (--no-asr)" if a.no_asr else
          f"ASR: {len(todo)} clip(s) to transcribe, estimated cost ${cost:.3f} (Meta Voice Transcribe, ~${ASR_USD_PER_CLIP}/clip)")

    for sub in ("footage", "asr", "audio"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
    # clips: copy as-is; a name clash between two different source files gets the parent folder prefixed
    used: dict[str, Path] = {}
    copied = 0
    for c in found["clips"]:
        name = c.name
        if name in used and used[name] != c:
            name = f"{c.parent.name}_{c.name}"
        used[name] = c
        target = dest / "footage" / name
        if not target.exists() or target.stat().st_size != c.stat().st_size:
            shutil.copy2(c, target)
            copied += 1

    def write(name: str, text: str) -> None:
        (dest / name).write_text(text.rstrip() + "\n")

    notes = []
    if found["brief"]:
        write("brief.md", read_text(found["brief"]))
    elif not (dest / "brief.md").exists():
        write("brief.md", ask(f"No brief found. Describe the brief for {a.name} (brand, goal, platform, tone, must-say/avoid):",
                              BRIEF_PLACEHOLDER.format(name=a.name), interactive))
        notes.append("brief.md was created from " + ("your input" if interactive else "the placeholder") + " - review it")
    subject = a.subject or (read_text(found["subject"]).strip() if found["subject"] else "")
    if subject:
        write("subject.txt", subject)
    elif not (dest / "subject.txt").exists():
        s = ask("No subject.txt. In one line, who is on camera (e.g. 'a man in his forties in a blue kurta, speaking to camera')?",
                "TODO: describe the person on camera in one line", interactive)
        write("subject.txt", s.splitlines()[0])
        notes.append("subject.txt is a placeholder - edit it" if s.startswith("TODO") else "subject.txt from your input")
    if found["refs"]:
        write("refs.md", "\n\n".join(f"## {p.name}\n{read_text(p).strip()}" for p in found["refs"]))

    n_rules = 0
    if found["rules"]:
        items, now = [], datetime.now(timezone.utc).isoformat(timespec="seconds")
        for p in found["rules"]:
            items += clients.parse_rules(read_text(p))
        for i in items:
            i.update(created_at=now, evidence={"doc_ref": "add_client rules"})
        (dest / "seed_memory.jsonl").write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
        n_rules = len(items)
        if not items:
            notes.append("rules file found but no line started with DO:/DON'T:/+/-")

    meta = {"id": a.id, "name": a.name, "subject": (dest / "subject.txt").read_text().strip(),
            "language": a.language, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    old = clients._read_meta(dest)
    meta["created_at"] = old.get("created_at", meta["created_at"])
    (dest / "client.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    done_asr = failed = 0
    if not a.no_asr and todo:
        from adfarming.footage import asr_for
        for f in sorted((dest / "footage").iterdir()):
            if f.suffix.lower() in clients.CLIP_EXT and not (dest / "asr" / f"{f.stem}.json").exists():
                try:
                    asr_for(f, dest / "audio", dest / "asr")
                    done_asr += 1
                except Exception as e:  # keep going; the clip is retried on first run
                    failed += 1
                    print(f"  ASR failed for {f.name}: {type(e).__name__}", file=sys.stderr)

    print(f"\nClient {a.id!r} ({a.name}) -> {dest}")
    print(f"  clips copied: {copied} of {len(found['clips'])} | rules seeded: {n_rules} | "
          f"ASR done: {done_asr}" + (f", failed: {failed}" if failed else "") + (" (skipped)" if a.no_asr else ""))
    for n in notes:
        print(f"  note: {n}")
    print("Next: restart the Studio (it discovers the client and seeds its memory), "
          f"then deploy/scripts/push_client_data.sh {dest} deploy@<IP>")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
