# -*- coding: utf-8 -*-
"""One-shot: pack ready games + compare fallen vs fallen2 (for 04:45 job)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import history_svc


def main() -> None:
    history_svc.ensure_dirs()
    history_svc.rebuild_index()

    ready = []
    seen = set()
    offset = 0
    while offset < 5000:
        page = history_svc.list_games(
            status="completed",
            packed="open",
            offset=offset,
            limit=100,
        )
        rows = page.get("games") or []
        if not rows:
            break
        for g in rows:
            gid = str(g.get("gameId") or "")
            if not gid or gid in seen or g.get("packedIn"):
                continue
            seen.add(gid)
            ready.append(gid)
        offset += len(rows)
        if offset >= int(page.get("total") or 0):
            break

    report = {
        "readyGames": len(ready),
        "pack": None,
        "compare": None,
        "error": None,
    }

    if not ready:
        report["error"] = "нет готовых незапакованных игр"
        report["compare"] = history_svc.compare_experience_agents(max_games=120)
        out = Path(__file__).resolve().parent / "history" / "compare_045_report.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    key = f"pack-045-{int(time.time())}-{','.join(sorted(ready)[:8])}"
    job = history_svc.start_pack_job(ready, idempotency_key=key)
    job_id = str((job or {}).get("jobId") or "")
    deadline = time.time() + 900
    final = job if isinstance(job, dict) else {}
    while time.time() < deadline and job_id:
        final = history_svc.job_status(job_id)
        st = str(final.get("status") or "")
        if st in (
            "done",
            "done_cleanup_pending",
            "ready",
            "error",
            "failed",
        ):
            break
        if final.get("packId") and st not in ("queued", "running", "cleanup"):
            break
        time.sleep(1.5)

    report["pack"] = {
        "jobId": job_id,
        "status": final.get("status"),
        "packId": final.get("packId") or (final.get("result") or {}).get("packId"),
        "filename": final.get("filename") or (final.get("result") or {}).get("filename"),
        "error": final.get("error"),
        "gameIds": ready,
    }
    report["compare"] = history_svc.compare_experience_agents(max_games=120)
    baseline_path = Path(__file__).resolve().parent / "history" / "baseline_before_045.json"
    if baseline_path.is_file():
        try:
            report["baseline"] = json.loads(baseline_path.read_text(encoding="utf-8"))
        except Exception as exc:
            report["baselineError"] = str(exc)

    # Compact verdict lines for chat
    lines = []
    lines.append(f"readyGames={report['readyGames']}")
    if report.get("pack"):
        lines.append(
            f"pack status={report['pack'].get('status')} "
            f"file={report['pack'].get('filename')}"
        )
    cmp = report.get("compare") or {}
    lines.append(f"verdict: {cmp.get('verdict')}")
    for a in cmp.get("agents") or []:
        if a.get("agent") not in ("fallen", "fallen2"):
            continue
        lines.append(
            f"{a['agent']}: games={a.get('games')} winRate={a.get('winRate')} "
            f"miss={a.get('missedTakePct')}% over={a.get('overpayPct')}% "
            f"error={a.get('errorRate')}%"
        )
    base = report.get("baseline") or {}
    if base.get("agents"):
        lines.append(f"baseline@{base.get('takenAt')}:")
        for a in base["agents"]:
            lines.append(
                f"  was {a['agent']}: winRate={a.get('winRate')} "
                f"error={a.get('errorRate')}% miss={a.get('missedTakePct')}% "
                f"over={a.get('overpayPct')}%"
            )
    report["summaryLines"] = lines

    out = Path(__file__).resolve().parent / "history" / "compare_045_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("---")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

