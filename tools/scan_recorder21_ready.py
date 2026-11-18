"""Скан desk/history: сколько журналов уже recorder 2.1 и сколько complete-раздач."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIST = ROOT / "desk" / "history" / "bots"


def _load_lines(path: Path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        t = line.strip()
        if not t:
            continue
        try:
            o = json.loads(t)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            yield o


def scan_file(path: Path) -> dict:
    meta = {}
    hand_ends = set()
    trump_with_hid = 0
    trump_total = 0
    confirmed = 0
    unconfirmed = 0
    for o in _load_lines(path):
        t = o.get("type")
        if t == "meta":
            meta = o
        if t == "trump":
            trump_total += 1
            if o.get("handId") is not None:
                trump_with_hid += 1
        if t in ("hand_end", "handEnd"):
            hid = o.get("handId")
            if hid is not None:
                hand_ends.add(str(hid))
        if t == "decision":
            if o.get("confirmed"):
                confirmed += 1
            else:
                unconfirmed += 1
    rv = meta.get("recorderVersion") or ""
    sv = meta.get("schemaVersion") or ""
    return {
        "path": str(path.relative_to(HIST.parent.parent)),
        "recorderVersion": rv,
        "schemaVersion": sv,
        "agent": meta.get("agent"),
        "agentVersion": meta.get("agentVersion"),
        "buildId": meta.get("buildId"),
        "format": meta.get("format"),
        "is_v21": bool(rv.startswith("2.1") or sv == "history-2.1"),
        "hand_ends": len(hand_ends),
        "trump_handId": f"{trump_with_hid}/{trump_total}",
        "decisions_confirmed": confirmed,
        "decisions_unconfirmed": unconfirmed,
        "mtime": path.stat().st_mtime,
    }


def main() -> int:
    files = sorted(HIST.rglob("game_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = [scan_file(p) for p in files]
    v21 = [r for r in rows if r["is_v21"]]
    old = [r for r in rows if not r["is_v21"]]
    ends_v21 = sum(r["hand_ends"] for r in v21)
    print(f"jsonl total: {len(rows)}")
    print(f"recorder 2.1 files: {len(v21)}  hand_end sum: {ends_v21}")
    print(f"old/unknown files: {len(old)}")
    print(f"ready_for_pack_20: {ends_v21 >= 20}")
    print("--- recent 12 ---")
    for r in rows[:12]:
        mark = "V21" if r["is_v21"] else "OLD"
        print(
            f"[{mark}] ends={r['hand_ends']:3d} rec={r['recorderVersion'] or '?':6s} "
            f"agent={r['agent']}:{r['agentVersion']} trumpHid={r['trump_handId']}  {r['path']}"
        )
    # summary for automation
    out = {
        "totalFiles": len(rows),
        "v21Files": len(v21),
        "v21HandEnds": ends_v21,
        "ready": ends_v21 >= 20,
        "versions": dict(Counter((r["recorderVersion"] or "?") for r in rows)),
    }
    (ROOT / "desk" / "history" / "recorder21_scan.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if ends_v21 >= 20 else 2


if __name__ == "__main__":
    sys.exit(main())
