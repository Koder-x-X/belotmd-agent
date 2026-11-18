# -*- coding: utf-8 -*-
"""04:30: soft-stop all desk bots — finish current game, no relaunch."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "history" / "stop_430_report.json"


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def main() -> None:
    report = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "stopped": [],
        "skipped": [],
        "errors": [],
    }
    try:
        payload = _req("GET", "/api/bots")
    except Exception as exc:
        report["errors"].append(f"list bots: {exc}")
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    bots = payload.get("bots") or []
    seen = set()
    for bot in bots:
        login = str(bot.get("login") or "")
        bid = str(bot.get("id") or "")
        if not bid or bid in seen:
            continue
        seen.add(bid)
        if not bot.get("running"):
            report["skipped"].append({"id": bid, "login": login, "reason": "not running"})
            continue
        try:
            res = _req("POST", f"/api/bots/{bid}/stop", {})
            report["stopped"].append({
                "id": bid,
                "login": login,
                "agent": bot.get("agent"),
                "remaining": res.get("remaining"),
                "ok": res.get("ok"),
            })
            print(f"soft-stop {login} ({bid}) remaining={res.get('remaining')}")
        except Exception as exc:
            report["errors"].append({"id": bid, "login": login, "error": str(exc)})
            print(f"FAIL {login}: {exc}")

    # session.json soft-stop already drops logins; ensure empty autostart
    session = Path(__file__).resolve().parent / "session.json"
    try:
        session.write_text(
            json.dumps({"bots": [], "updatedAt": datetime.now().timestamp(),
                        "note": "cleared at 04:30 soft-stop — start manually"},
                       ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        report["sessionCleared"] = True
    except Exception as exc:
        report["sessionCleared"] = False
        report["errors"].append(f"session: {exc}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
