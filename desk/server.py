"""Стол belotmd_agent: профили, выбор агента, сравнение кто выиграл больше.

Только 127.0.0.1. Cookies остаются в процессе бота, в браузер не уходят.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
RUNS = ROOT / "runs"
PROFILES_FILE = ROOT / "profiles.json"
SESSION_FILE = ROOT / "session.json"  # кого поднимать после перезапуска стола
AGENT_PKG = ROOT.parent  # belotmd_agent
WORKSPACE = AGENT_PKG.parent
BUILDS = {
    "3": AGENT_PKG,              # strong / heuristic / random
    "4": WORKSPACE / "belotmd",  # пары, если папка есть
}
HOST = os.environ.get("BELOT_DESK_HOST", "127.0.0.1")
PORT = int(os.environ.get("BELOT_DESK_PORT", "8790"))
LOGIN_URL = "https://belot.md/action.php"
HOME_URL = "https://belot.md/"
MAX_BOTS = 100
SWITCH_WAIT_S = 10  # пауза перед повторной посадкой при смене агента
RESTORE_GAP_S = 2.0  # пауза между автопосадками после рестарта
HISTORY_DIR = Path(os.environ.get("BELOT_HISTORY_DIR") or (ROOT / "history"))

import history_svc  # noqa: E402  — рядом с server.py

# RLock: soft-stop/cancel вызывают _push, уже держа замок списка ботов.
_lock = threading.RLock()
_bots: dict[str, dict] = {}
_profiles_lock = threading.Lock()
_session_lock = threading.Lock()
_belot_profile_lock = threading.Lock()
_belot_profile_cache: dict[str, dict] = {}  # login.casefold() -> profile dict
BELOT_PROFILE_TTL_S = 300.0
_seq = 0
_restoring = False
_HAND = re.compile(
    r"\[стол\] раздача \d+ \| партия (\S+) \| очки (\S+) \| комбинации (\S+) \| "
    r"капот (да|нет) \| места (.*) \| итог (игра|победа|поражение)"
)
_BOARD = re.compile(r"\[стол\] табло (.+)$")


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _load_profiles() -> list[dict]:
    if not PROFILES_FILE.exists():
        return []
    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("profiles") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        login = str(row.get("login") or "").strip()
        password = str(row.get("password") or "")
        if not login or not password:
            continue
        out.append({
            "login": login,
            "password": password,
            "agent": str(row.get("agent") or "strong"),
            "variant": str(row.get("variant") or "3"),
            "points": str(row.get("points") or ""),
            "once": bool(row.get("once", False)),
            "updatedAt": float(row.get("updatedAt") or 0),
        })
    out.sort(key=lambda r: (r.get("updatedAt") or 0), reverse=True)
    return out


def _save_profiles(rows: list[dict]) -> None:
    payload = {"profiles": rows, "updatedAt": time.time()}
    tmp = PROFILES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROFILES_FILE)


def _upsert_profile(login: str, password: str, *, agent: str, variant: str,
                    points: str, once: bool) -> dict:
    login = login.strip()
    if not login or not password:
        raise RuntimeError("Для сохранения нужны логин и пароль.")
    with _profiles_lock:
        rows = _load_profiles()
        found = None
        for row in rows:
            if row["login"].casefold() == login.casefold():
                found = row
                break
        if found is None:
            found = {"login": login}
            rows.append(found)
        found["login"] = login
        found["password"] = password
        found["agent"] = agent or "strong"
        found["variant"] = variant if variant in BUILDS else "3"
        found["points"] = points if points in ("", "51", "101") else ""
        found["once"] = bool(once)
        found["updatedAt"] = time.time()
        _save_profiles(rows)
        return dict(found)


def _delete_profile(login: str) -> bool:
    login = (login or "").strip()
    if not login:
        return False
    with _profiles_lock:
        rows = _load_profiles()
        keep = [r for r in rows if r["login"].casefold() != login.casefold()]
        if len(keep) == len(rows):
            return False
        _save_profiles(keep)
        return True


def _session_row(bot: dict) -> dict:
    row = {
        "login": bot["login"],
        "password": bot["password"],
        "agent": bot.get("agent") or "strong",
        "variant": str(bot.get("variant") or "3"),
        "points": str(bot.get("points") or ""),
        "once": bool(bot.get("once")),
        "rejoinDelay": float(bot.get("rejoinDelay") or 5),
        "maxRounds": int(bot.get("maxRounds") or 0),
        "reconnect": bool(bot.get("reconnect", True)),
    }
    # Private trio / join-by-host: persist so автозапуск reseats the squad.
    for key in ("tableMode", "tableCreator", "tablePassword"):
        val = str(bot.get(key) or "").strip()
        if val:
            row[key] = val
    return row


def _load_session() -> list[dict]:
    if not SESSION_FILE.exists():
        return []
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("bots") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        login = str(row.get("login") or "").strip()
        password = str(row.get("password") or "")
        if not login or not password:
            continue
        key = login.casefold()
        if key in seen:
            continue
        seen.add(key)
        item = {
            "login": login,
            "password": password,
            "agent": str(row.get("agent") or "strong"),
            "variant": str(row.get("variant") or "3"),
            "points": str(row.get("points") or ""),
            "once": bool(row.get("once", False)),
            "rejoinDelay": float(row.get("rejoinDelay") or 5),
            "maxRounds": int(row.get("maxRounds") or 0),
            "reconnect": bool(row.get("reconnect", True)),
        }
        for key in ("tableMode", "tableCreator", "tablePassword"):
            val = str(row.get(key) or "").strip()
            if val:
                item[key] = val
        out.append(item)
    return out


def _save_session(rows: list[dict]) -> None:
    payload = {"bots": rows, "updatedAt": time.time()}
    tmp = SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SESSION_FILE)


def _session_upsert(bot: dict) -> None:
    """Запомнить бота, чтобы после рестарта стола посадить снова."""
    if bot.get("once"):
        return
    row = _session_row(bot)
    with _session_lock:
        rows = _load_session()
        keep = [r for r in rows if r["login"].casefold() != row["login"].casefold()]
        keep.append(row)
        _save_session(keep)


def _session_drop_login(login: str) -> None:
    """Убрать из автозапуска (пользователь сам остановил)."""
    key = (login or "").strip().casefold()
    if not key:
        return
    with _session_lock:
        rows = _load_session()
        keep = [r for r in rows if r["login"].casefold() != key]
        if len(keep) != len(rows):
            _save_session(keep)


def _kill_orphan_belotmd() -> int:
    """Убить осиротевшие процессы ботов после рестарта стола."""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    (
                        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                        "Where-Object { $_.CommandLine -match '-m\\s+belotmd' } | "
                        "ForEach-Object { "
                        "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
                        "'1' }"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return len([ln for ln in (completed.stdout or "").splitlines() if ln.strip() == "1"])
        completed = subprocess.run(
            ["pkill", "-f", r"python.*-m belotmd"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        # pkill: 0 = killed something, 1 = none matched
        return 1 if completed.returncode == 0 else 0
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _restore_session_bg() -> None:
    """После старта стола: очистить сирот и посадить сохранённых ботов."""
    global _restoring
    rows = _load_session()
    orphans = _kill_orphan_belotmd()
    if orphans:
        print(f"[стол] снял {orphans} старых процессов ботов", flush=True)
        time.sleep(max(SWITCH_WAIT_S, 8))
    if not rows:
        print("[стол] автозапуск: некого сажать (session.json пуст)", flush=True)
        return
    _restoring = True
    print(f"[стол] автозапуск {len(rows)} бот(ов) из session.json", flush=True)
    try:
        for i, row in enumerate(rows):
            try:
                result = _start(
                    str(row.get("variant") or "3"),
                    str(row.get("agent") or "strong"),
                    str(row["login"]),
                    str(row["password"]),
                    bool(row.get("once")),
                    rejoin_delay=float(row.get("rejoinDelay") or 5),
                    max_rounds=int(row.get("maxRounds") or 0),
                    reconnect=bool(row.get("reconnect", True)),
                    points=str(row.get("points") or ""),
                    table_mode=str(row.get("tableMode") or ""),
                    table_creator=str(row.get("tableCreator") or ""),
                    table_password=str(row.get("tablePassword") or ""),
                )
                print(f"[стол] авто: {row['login']} → {row.get('agent')} ({result.get('id')})", flush=True)
            except Exception as exc:
                print(f"[стол] авто: {row.get('login')} не сел: {exc}", flush=True)
            if i + 1 < len(rows):
                time.sleep(RESTORE_GAP_S)
    finally:
        _restoring = False


def _tally(bot: dict, text: str) -> None:
    stats = bot.setdefault(
        "tally",
        {
            "hands": 0, "actions": 0, "tables": 0, "lostSeats": 0, "errors": 0,
            "score": "", "wins": 0, "losses": 0, "handPoints": "", "combos": "",
            "capot": "", "seats": "",
            "scoreboard": None,
        },
    )
    deal = _HAND.search(text)
    if deal:
        stats["hands"] += 1
        stats["score"] = deal.group(1)
        stats["handPoints"] = deal.group(2)
        stats["combos"] = deal.group(3)
        stats["capot"] = deal.group(4)
        stats["seats"] = deal.group(5).strip()
        if deal.group(6) == "победа":
            stats["wins"] += 1
        elif deal.group(6) == "поражение":
            stats["losses"] += 1
    board = _BOARD.search(text)
    if board:
        try:
            payload = json.loads(board.group(1))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("names"), list):
            rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
            stats["scoreboard"] = {
                "names": [str(n) for n in payload["names"]],
                "rows": [[str(c) for c in row] for row in rows if isinstance(row, list)],
            }
    if "Bot Action" in text or "[Trick]" in text:
        stats["actions"] += 1
    if "Joined Game Room" in text:
        stats["tables"] += 1
    if "SEAT LOST" in text:
        stats["lostSeats"] += 1
    if "SDK Error" in text or "[Bot][CRITICAL]" in text:
        stats["errors"] += 1
    match = re.search(r"totals now \((\d+),(\d+)\)", text)
    if match:
        stats["score"] = f"{match.group(1)}:{match.group(2)}"


def _keep_line(text: str) -> bool:
    noise = (
        "card conservation",
        "AUDIT:VIOLATION",
        "AUDIT:PROBE",
        "[Sync]",
        "[MSG]",
        "suppressed: IndexError",
    )
    return not any(bit in text for bit in noise)


def _push(bot: dict, line: str) -> None:
    text = line.rstrip("\r\n")
    if not text:
        return
    with _lock:
        if text.startswith("[LIVE]"):
            raw = text[6:].strip()
            try:
                bot["table"] = json.loads(raw)
                bot["tableAt"] = time.time()
            except json.JSONDecodeError:
                pass
            return
        if text.startswith("[HISTORY]"):
            raw = text[9:].strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return
            if isinstance(payload, dict) and not str(payload.get("op") or "").startswith("ERROR"):
                try:
                    history_svc.apply_history_line(payload)
                except Exception:
                    pass
            return
        _tally(bot, text)
        if not _keep_line(text):
            return
        bot["lines"].append({"t": _stamp(), "text": text})
        if len(bot["lines"]) > 800:
            del bot["lines"][: len(bot["lines"]) - 800]


def _agents(variant: str) -> list[str]:
    folder = BUILDS.get(variant)
    if folder is None or not folder.is_dir():
        return ["random"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(folder / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "belotmd", "--list-agents"],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return names or ["random"]


def _login(username: str, password: str, proxy: str = "") -> str:
    del proxy  # desk always logs in directly
    jar = CookieJar()
    handlers: list = [urllib.request.HTTPCookieProcessor(jar)]
    opener = urllib.request.build_opener(*handlers)
    payload = urllib.parse.urlencode(
        {"do": "login", "login": username, "pass": password}
    ).encode()
    request = urllib.request.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "belotmd-agent-desk",
        },
    )
    try:
        with opener.open(request, timeout=15) as response:
            body = response.read().decode("utf-8", "replace").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Сайт не ответил: {exc.reason}") from exc

    if body == "1":
        raise RuntimeError("Слишком много попыток входа. Подождите и попробуйте снова.")
    if body == "2":
        raise RuntimeError("Неверный логин или пароль.")

    found = {cookie.name: cookie.value for cookie in jar}
    # Home page is optional; on VDS it often hangs and must not block seat.
    if not (found.get("PHPSESSID") and found.get("token")):
        try:
            with opener.open(HOME_URL, timeout=8) as home:
                home.read(4096)
        except Exception:
            pass
        found = {cookie.name: cookie.value for cookie in jar}

    session = found.get("PHPSESSID", "")
    token = found.get("token", "")
    if not session or not token:
        raise RuntimeError("Сайт не отдал сессию. Проверьте логин и пароль.")
    return f"PHPSESSID={session}; token={token};"


def _alive(bot: dict) -> bool:
    proc = bot.get("proc")
    return proc is not None and proc.poll() is None


def _freeze(bot: dict) -> None:
    bot.setdefault("stoppedAt", time.time())


def _score_parts(score: str) -> list[int]:
    parts = []
    for bit in str(score or "").replace(";", ":").split(":"):
        bit = bit.strip()
        if bit.isdigit():
            parts.append(int(bit))
    return parts


def _target_points(bot: dict) -> int | None:
    raw = str(bot.get("points") or "").strip()
    if raw in ("51", "101"):
        return int(raw)
    if bot.get("variant") == "4":
        return 101
    return None


def _remaining(bot: dict) -> int | None:
    target = _target_points(bot)
    if target is None:
        return None
    parts = _score_parts((bot.get("tally") or {}).get("score") or "")
    if not parts:
        return target
    return max(0, target - max(parts))


def _phase(bot: dict) -> str:
    if bot.get("switching"):
        return "switching"
    if not _alive(bot):
        return "stopped"
    if bot.get("stopping"):
        return "stopping"
    for row in reversed(bot["lines"]):
        text = row["text"]
        if "Bot Action" in text or "Joined Game Room" in text:
            return "playing"
        if (
            "looking for another table" in text
            or "Searching public tables" in text
            or "No public open tables" in text
        ):
            return "waiting"
        if "Session ended" in text or "бот остановился" in text:
            return "stopped"
    return "starting"


def _switch_left(bot: dict) -> int:
    """Секунд до повторной посадки при смене агента."""
    eta = float(bot.get("switchEta") or 0)
    if not bot.get("switching") or eta <= 0:
        return 0
    return max(0, int(round(eta - time.time())))


def _stats(bot: dict) -> dict:
    tally = bot.get("tally")
    if tally:
        return dict(tally)
    texts = [row["text"] for row in bot["lines"]]
    score = ""
    for text in texts:
        match = re.search(r"totals now \((\d+),(\d+)\)", text)
        if match:
            score = f"{match.group(1)}:{match.group(2)}"
    return {
        "hands": sum("hand scored" in text for text in texts),
        "actions": sum("Bot Action" in text for text in texts),
        "tables": sum("Joined Game Room" in text for text in texts),
        "lostSeats": sum("SEAT LOST" in text for text in texts),
        "errors": sum("SDK Error" in text or "[Bot][CRITICAL]" in text for text in texts),
        "score": score,
        "wins": 0,
        "losses": 0,
    }


def _belot_profile_public(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    fetched = float(row.get("fetchedAt") or 0)
    stale = (time.time() - fetched) > BELOT_PROFILE_TTL_S if fetched else True
    return {
        "ok": bool(row.get("ok")),
        "place": row.get("place"),
        "ratingStars": row.get("ratingStars"),
        "statusName": row.get("statusName"),
        "statusId": row.get("statusId"),
        "statusIcon": row.get("statusIcon"),
        "coins": row.get("coins"),
        "todayWins": row.get("todayWins"),
        "todayGames": row.get("todayGames"),
        "todayError": row.get("todayError"),
        "error": row.get("error"),
        "fetchedAt": fetched,
        "stale": stale,
        "source": row.get("source") or "",
        "inGame": bool(row.get("inGame") or row.get("error") == "in_game"),
    }


def _belot_profile_peek(login: str) -> dict | None:
    key = (login or "").strip().casefold()
    if not key:
        return None
    with _belot_profile_lock:
        return _belot_profile_public(_belot_profile_cache.get(key))


def _ingest_belot_profile(data: dict) -> dict:
    """Принять снимок от бота (после партии) без повторного login."""
    login = str(data.get("login") or "").strip()
    if not login:
        raise RuntimeError("Нужен login.")
    key = login.casefold()
    now = time.time()
    ok = bool(data.get("ok", True))
    row = {
        "ok": ok,
        "login": login,
        "place": data.get("place"),
        "ratingStars": data.get("ratingStars"),
        "statusName": data.get("statusName"),
        "statusId": data.get("statusId"),
        "statusIcon": data.get("statusIcon"),
        "coins": data.get("coins"),
        "todayWins": data.get("todayWins"),
        "todayGames": data.get("todayGames"),
        "todayError": data.get("todayError"),
        "error": data.get("error"),
        "fetchedAt": now,
        "source": str(data.get("source") or "push"),
        "inGame": False,
    }
    # Числовые поля из JSON могут прийти строкой
    for num_key in ("place", "ratingStars", "statusId", "coins", "todayWins", "todayGames"):
        val = row.get(num_key)
        if val is None or val == "":
            row[num_key] = None
            continue
        try:
            row[num_key] = int(val)
        except (TypeError, ValueError):
            pass
    if ok and all(row.get(k) is None for k in ("place", "ratingStars", "coins", "statusName")):
        row["ok"] = False
        row["error"] = row.get("error") or "empty_push"
    with _belot_profile_lock:
        if not row["ok"]:
            prev = _belot_profile_cache.get(key)
            if isinstance(prev, dict) and prev.get("ok"):
                kept = dict(prev)
                kept["checkedAt"] = now
                kept["pushError"] = row.get("error")
                _belot_profile_cache[key] = kept
                return dict(kept)
        _belot_profile_cache[key] = row
    return dict(row)


def _fetch_belot_profile(login: str, password: str, *, force: bool = False) -> dict:
    """Отключено: опросы belot.md (рейтинг) банили IP. Кэш только read-only."""
    del password, force
    login = (login or "").strip()
    key = login.casefold()
    with _belot_profile_lock:
        hit = _belot_profile_cache.get(key)
        if hit:
            return dict(hit)
    return {
        "ok": False,
        "login": login,
        "error": "профиль belot.md отключён",
        "fetchedAt": time.time(),
    }


def _running_bot_accounts() -> list[dict]:
    """Уникальные включённые аккаунты (login/password/agent/…)."""
    seen: set[str] = set()
    rows: list[dict] = []
    with _lock:
        bots = list(_bots.values())
    for bot in bots:
        if not (_alive(bot) or bot.get("switching")):
            continue
        key = str(bot.get("login") or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "id": bot["id"],
                "login": bot["login"],
                "password": bot["password"],
                "agent": bot.get("agent") or "",
                "phase": _phase(bot),
                "running": True,
                "variant": bot.get("variant"),
                "points": bot.get("points") or "",
            }
        )
    rows.sort(key=lambda r: str(r["login"]).casefold())
    return rows


def _ratings_accounts(*, include_saved: bool = True) -> list[dict]:
    """Включённые боты + (опционально) сохранённые профили не за столом."""
    rows = _running_bot_accounts()
    seen = {str(r["login"]).casefold() for r in rows}
    if not include_saved:
        return rows
    for prof in _load_profiles():
        login = str(prof.get("login") or "").strip()
        key = login.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "id": "",
                "login": login,
                "password": str(prof.get("password") or ""),
                "agent": str(prof.get("agent") or ""),
                "phase": "saved",
                "running": False,
                "variant": str(prof.get("variant") or "3"),
                "points": str(prof.get("points") or ""),
            }
        )
    return rows


def _public(bot: dict, with_lines: bool = False, after: int = 0) -> dict:
    running = _alive(bot)
    switching = bool(bot.get("switching"))
    if not running and not switching:
        _freeze(bot)
    payload = {
        "id": bot["id"],
        "login": bot["login"],
        "password": bot["password"],
        "variant": bot["variant"],
        "agent": bot["agent"],
        "once": bot["once"],
        "rejoinDelay": bot.get("rejoinDelay", 5),
        "maxRounds": bot.get("maxRounds", 0),
        "reconnect": bot.get("reconnect", True),
        "points": bot.get("points") or "",
        "tableMode": str(bot.get("tableMode") or ""),
        "tableCreator": str(bot.get("tableCreator") or ""),
        "tablePrivate": bool(str(bot.get("tablePassword") or "").strip()),
        "startedAt": bot["startedAt"],
        "stoppedAt": bot.get("stoppedAt") or 0,
        "running": running,
        "stopping": bool(bot.get("stopping")) and running,
        "switching": switching,
        "switchAgent": bot.get("switchAgent") or "",
        "switchIn": _switch_left(bot) if switching else 0,
        "remaining": _remaining(bot),
        "target": _target_points(bot),
        "phase": _phase(bot),
        "stats": _stats(bot),
        "table": bot.get("table"),
        "tableAt": bot.get("tableAt") or 0,
        "marks": int(bot.get("marksCount") or 0),
        "belotProfile": _belot_profile_peek(str(bot.get("login") or "")),
        "proxy": bot.get("proxyHost") or "",
        "proxyUrl": bot.get("proxyRedacted") or "",
    }
    if with_lines:
        rows = bot["lines"][after:]
        payload["lines"] = rows
        payload["next"] = after + len(rows)
    return payload


def _compare_agents() -> dict:
    """Сводка по агентам: какой чаще выигрывает на профилях стола."""
    by_agent: dict[str, dict] = {}
    with _lock:
        bots = list(_bots.values())
    for bot in bots:
        name = bot.get("agent") or "?"
        row = by_agent.setdefault(
            name,
            {
                "agent": name,
                "profiles": 0,
                "running": 0,
                "wins": 0,
                "losses": 0,
                "hands": 0,
                "actions": 0,
                "tables": 0,
                "lostSeats": 0,
                "errors": 0,
                "logins": [],
                "profileStats": [],
            },
        )
        row["profiles"] += 1
        alive = _alive(bot)
        if alive:
            row["running"] += 1
        st = _stats(bot)
        for key in ("wins", "losses", "hands", "actions", "tables", "lostSeats", "errors"):
            row[key] += int(st.get(key) or 0)
        login = bot.get("login") or ""
        if login and login not in row["logins"]:
            row["logins"].append(login)
        row["profileStats"].append({
            "id": bot.get("id") or "",
            "login": login or "?",
            "running": alive,
            "phase": _phase(bot) if alive else "stopped",
            "wins": int(st.get("wins") or 0),
            "losses": int(st.get("losses") or 0),
            "hands": int(st.get("hands") or 0),
            "score": st.get("score") or "",
            "errors": int(st.get("errors") or 0),
        })

    agents = []
    for row in by_agent.values():
        decided = row["wins"] + row["losses"]
        row["winRate"] = round(100.0 * row["wins"] / decided, 1) if decided else None
        row["decided"] = decided
        row["profileStats"].sort(
            key=lambda p: (p["wins"], -p["losses"], p["hands"]),
            reverse=True,
        )
        agents.append(row)
    agents.sort(
        key=lambda r: (
            r["wins"],
            r["winRate"] if r["winRate"] is not None else -1,
            -r["losses"],
            r["hands"],
        ),
        reverse=True,
    )
    # Лидер — у кого больше побед (при равенстве смотрим % и меньше поражений).
    top = agents[0] if agents else None
    best = top["agent"] if top and int(top.get("wins") or 0) > 0 else None
    return {
        "agents": agents,
        "best": best,
        "note": "Сравнение по победам профилей этого стола. "
                "Разные столы и сила соперников влияют на результат.",
    }


def _reader(bot: dict) -> None:
    proc = bot["proc"]
    assert proc.stdout is not None
    for line in proc.stdout:
        _push(bot, line)
        limit = int(bot.get("maxRounds") or 0)
        if limit and int(bot.get("tally", {}).get("hands") or 0) >= limit:
            with _lock:
                _soft_stop(bot)
            break
    code = proc.wait()
    with _lock:
        _freeze(bot)
        bot["stopping"] = False
        if bot.get("proc") is proc:
            bot["proc"] = None
    _push(bot, f"[стол] бот остановился, код {code}")


def _soft_stop(bot: dict) -> bool:
    if not _alive(bot):
        return False
    if bot.get("stopping"):
        return True
    bot["stopping"] = True
    bot["keepAlive"] = False
    _session_drop_login(str(bot.get("login") or ""))
    stop_path = Path(bot.get("stopFile") or (RUNS / f"{bot['id']}.stop"))
    bot["stopFile"] = str(stop_path)
    try:
        stop_path.write_text("stop\n", encoding="utf-8")
    except OSError as exc:
        bot["stopping"] = False
        raise RuntimeError(f"Не удалось попросить бота остановиться: {exc}") from exc
    # Без CTRL_BREAK/SIGINT: бот сам читает stop-файл и доигрывает партию.
    # Сигнал раньше ронял процесс до отмены остановки из UI.
    left = _remaining(bot)
    if left is None:
        note = "остановка после этой партии"
    elif left == 0:
        note = "остановка: партия уже на финише"
    else:
        note = f"остановка после этой партии, осталось около {left}"
    _push(bot, f"[стол] {note}")
    return True


def _cancel_stop(bot: dict) -> bool:
    """Отменить soft-stop: бот продолжает после текущей партии."""
    if not _alive(bot):
        return False
    if not bot.get("stopping"):
        return True
    stop_path = Path(bot.get("stopFile") or (RUNS / f"{bot['id']}.stop"))
    bot["stopFile"] = str(stop_path)
    try:
        stop_path.write_text("go\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Не удалось отменить остановку: {exc}") from exc
    bot["stopping"] = False
    bot["keepAlive"] = True
    _session_upsert(bot)
    _push(bot, "[стол] остановку отменили — бот продолжает")
    return True


def _hard_stop(bot: dict) -> None:
    proc = bot.get("proc")
    bot["proc"] = None
    bot["stopping"] = False
    _freeze(bot)
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _points(variant: str, raw) -> str:
    if variant != "3":
        return ""
    text = str(raw or "").strip()
    if text in ("", "all"):
        return ""
    if text not in ("51", "101"):
        raise RuntimeError("Для троих выберите все столы, до 51 или до 101.")
    return text


def _spawn_process(bot: dict, cookies: str, agent: str) -> None:
    """Запустить процесс бота в уже созданной записи."""
    variant = str(bot["variant"])
    folder = BUILDS[variant]
    if not folder.is_dir():
        raise RuntimeError(
            f"Сборка для {variant}p не найдена: {folder}. "
            f"Для четверых нужна папка belotmd рядом с belotmd_agent."
        )
    bot_id = bot["id"]
    once = bool(bot.get("once"))
    reconnect = bool(bot.get("reconnect", True)) and not once
    rejoin_delay = float(bot.get("rejoinDelay") or 5)
    points = str(bot.get("points") or "")
    RUNS.mkdir(exist_ok=True)
    stop_file = Path(bot.get("stopFile") or (RUNS / f"{bot_id}.stop"))
    bot["stopFile"] = str(stop_file)
    if stop_file.exists():
        stop_file.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(folder / "src")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["BELOT_COOKIES"] = cookies
    env["BELOT_AGENT"] = agent
    env["BELOT_FRAMES"] = str(RUNS / f"{bot_id}.jsonl")
    env["BELOT_STOP_FILE"] = str(stop_file)
    env["BELOT_REJOIN_DELAY"] = str(rejoin_delay)
    env["BELOT_RETRY_DELAY"] = "15"
    # Do NOT skip/abandon active rooms on every start — that kicks us mid-match
    # and lets the site bot sit. Private-squad starts always skip: stale
    # OTHER_SESSION rooms block the host from creating the password table.
    if str(bot.get("tablePassword") or "").strip():
        env["BELOT_SKIP_ACTIVE"] = "1"
    else:
        env["BELOT_SKIP_ACTIVE"] = os.environ.get("BELOT_SKIP_ACTIVE", "0")
    # Never tunnel localhost bridge through a system HTTP proxy.
    for k in (
        "BELOT_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(k, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]
    bot["proxy"] = ""
    bot["proxyHost"] = ""
    bot["proxyRedacted"] = ""
    # Prefer hosting our own table. If create is refused, the client sits at
    # an existing open table, or waits until a free seat appears — unless a
    # private password is set (squad self-play must not leak to public lobby).
    table_mode = str(bot.get("tableMode") or os.environ.get("BELOT_TABLE_MODE") or "create").strip()
    table_creator = str(bot.get("tableCreator") or "").strip()
    table_password = str(bot.get("tablePassword") or "").strip()
    env["BELOT_TABLE_MODE"] = table_mode or "create"
    if table_creator:
        env["BELOT_TABLE_CREATOR"] = table_creator
    else:
        env.pop("BELOT_TABLE_CREATOR", None)
    if table_password:
        env["BELOT_TABLE_PASSWORD"] = table_password
    else:
        env.pop("BELOT_TABLE_PASSWORD", None)
    if table_mode == "join":
        # Host may still be creating; give guests a wider window.
        env["BELOT_JOIN_MAX_POLLS"] = os.environ.get("BELOT_JOIN_MAX_POLLS", "40")
    env["BELOT_RECONNECT"] = "0" if once or not reconnect else "1"
    # Рейтинг/шапка belot.md отключены: лишние HTTP → бан IP.
    env.pop("BELOT_PROFILE_AFTER_MATCH", None)
    env["BELOT_DESK_URL"] = f"http://{HOST}:{PORT}"
    if points:
        env["BELOT_TABLE_POINTS"] = points
    env["BELOT_DECISION_JOURNAL"] = "1"
    env["BELOT_DECISIONS"] = str(RUNS / f"{bot_id}-decisions.jsonl")
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    env["BELOT_HISTORY_DIR"] = str(HISTORY_DIR)
    env["BELOT_HISTORY_LOGIN"] = str(bot.get("login") or "")
    env["BELOT_HISTORY_VARIANT"] = str(variant)
    try:
        from belotmd.history import stable_bot_id as _stable_bot_id
        env["BELOT_HISTORY_BOT_ID"] = _stable_bot_id(str(bot.get("login") or ""))
    except Exception:
        try:
            sys.path.insert(0, str(AGENT_PKG / "src"))
            from belotmd.history import stable_bot_id as _stable_bot_id
            env["BELOT_HISTORY_BOT_ID"] = _stable_bot_id(str(bot.get("login") or ""))
        except Exception:
            # VDS/minimal install may omit history package — bots still play.
            env["BELOT_HISTORY_BOT_ID"] = str(bot.get("login") or bot_id)
    bot["historyBotId"] = env["BELOT_HISTORY_BOT_ID"]
    command = [sys.executable, "-m", "belotmd", "--agent", agent]
    if once:
        command.append("--once")
    popen_kw: dict = {
        "cwd": folder,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(command, **popen_kw)
    bot["proc"] = proc
    bot["agent"] = agent
    bot["startedAt"] = time.time()
    bot.pop("stoppedAt", None)
    bot["stopping"] = False
    bot["keepAlive"] = True
    label = "на троих" if variant == "3" else "на четверых"
    limit = f" до {points}" if points else ""
    mode = str(bot.get("tableMode") or "create")
    seat = f", mode={mode}"
    if mode == "join" and bot.get("tableCreator"):
        seat += f"@{bot['tableCreator']}"
    if bot.get("tablePassword"):
        seat += " private"
    _push(bot, f"[стол] сел {agent}, стол {label}{limit}{seat}, аккаунт {bot['login']}")
    _session_upsert(bot)
    threading.Thread(target=_reader, args=(bot,), daemon=True).start()


def _clear_switch(bot: dict) -> None:
    bot["switching"] = False
    bot.pop("switchAgent", None)
    bot.pop("switchEta", None)
    bot.pop("switchToken", None)


def _switch_agent(bot: dict, agent: str) -> dict:
    """Остановить бота, подождать SWITCH_WAIT_S и посадить с новым агентом."""
    variant = str(bot.get("variant") or "3")
    names = _agents(variant)
    if agent not in names:
        raise RuntimeError(
            f"Агента {agent!r} нет в этой сборке. Доступны: {', '.join(names)}"
        )
    if bot.get("switching"):
        raise RuntimeError("Смена агента уже идёт. Дождитесь паузы или отмените.")
    old = str(bot.get("agent") or "?")
    if agent == old and _alive(bot):
        raise RuntimeError(f"Уже работает агент {agent}.")

    token = f"{bot['id']}-{time.time_ns()}"
    bot["switching"] = True
    bot["switchAgent"] = agent
    bot["switchEta"] = time.time() + SWITCH_WAIT_S
    bot["switchToken"] = token
    bot["stopping"] = False
    stop_path = Path(bot.get("stopFile") or (RUNS / f"{bot['id']}.stop"))
    try:
        if stop_path.exists():
            stop_path.unlink(missing_ok=True)
    except OSError:
        pass
    _hard_stop(bot)
    _push(
        bot,
        f"[стол] пауза {SWITCH_WAIT_S}с — смена агента {old} → {agent}",
    )
    threading.Thread(
        target=_switch_worker, args=(bot["id"], token, agent), daemon=True
    ).start()
    try:
        _upsert_profile(
            bot["login"], bot["password"],
            agent=agent, variant=variant,
            points=str(bot.get("points") or ""),
            once=bool(bot.get("once")),
        )
    except Exception:
        pass
    return {
        "id": bot["id"],
        "wait": SWITCH_WAIT_S,
        "from": old,
        "agent": agent,
    }


def _switch_worker(bot_id: str, token: str, agent: str) -> None:
    time.sleep(SWITCH_WAIT_S)
    with _lock:
        bot = _bots.get(bot_id)
        if bot is None:
            return
        if not bot.get("switching") or bot.get("switchToken") != token:
            return  # отменили или новая смена
        if _alive(bot):
            _clear_switch(bot)
            return
        login = bot["login"]
        password = bot["password"]
    try:
        cookies = _login(login, password, proxy="")
    except Exception as exc:
        with _lock:
            bot = _bots.get(bot_id)
            if bot is not None and bot.get("switchToken") == token:
                _clear_switch(bot)
                _push(bot, f"[стол] смена агента не удалась: {exc}")
        return
    with _lock:
        bot = _bots.get(bot_id)
        if bot is None or bot.get("switchToken") != token:
            return
        try:
            bot["proxy"] = ""
            bot["proxyHost"] = ""
            bot["proxyRedacted"] = ""
            _spawn_process(bot, cookies, agent)
            _clear_switch(bot)
            _push(bot, f"[стол] агент сменён на {agent}")
        except Exception as exc:
            _clear_switch(bot)
            _push(bot, f"[стол] смена агента не удалась: {exc}")


def _cancel_switch(bot: dict) -> bool:
    if not bot.get("switching"):
        return False
    _clear_switch(bot)
    _push(bot, "[стол] смену агента отменили — бот остаётся выключенным")
    return True


def _save_mark(
    bot: dict,
    *,
    note: str,
    better: str = "",
    better_card: str = "",
    about_trick: dict | None = None,
) -> dict:
    """Сохранить разбор ошибки для будущего обучения."""
    note = str(note or "").strip()
    if not note:
        raise RuntimeError("Напишите, что было не так.")
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{bot['id']}-marks.jsonl"
    rec = {
        "t": time.time(),
        "stamp": _stamp(),
        "botId": bot["id"],
        "login": bot.get("login"),
        "agent": bot.get("agent"),
        "variant": bot.get("variant"),
        "note": note,
        "better": str(better or "").strip(),
        "betterCard": str(better_card or "").strip(),
        "aboutTrick": about_trick if isinstance(about_trick, dict) else None,
        "table": bot.get("table"),
        "decision": (bot.get("table") or {}).get("decision") if isinstance(bot.get("table"), dict) else None,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    bot["marksCount"] = int(bot.get("marksCount") or 0) + 1
    short = note if len(note) <= 80 else note[:77] + "…"
    _push(bot, f"[урок] {short}")
    return {"ok": True, "marks": bot["marksCount"], "path": str(path)}


def _start(variant: str, agent: str, username: str, password: str, once: bool,
          rejoin_delay: float = 5, max_rounds: int = 0, reconnect: bool = True,
          points: str = "", table_mode: str = "", table_creator: str = "",
          table_password: str = "") -> dict:
    if variant not in BUILDS:
        raise RuntimeError("Выберите стол на 3 или на 4.")
    folder = BUILDS[variant]
    if not folder.is_dir():
        raise RuntimeError(
            f"Сборка для {variant}p не найдена: {folder}. "
            f"Для четверых нужна папка belotmd рядом с belotmd_agent."
        )
    points = _points(variant, points)
    names = _agents(variant)
    if agent not in names:
        raise RuntimeError(
            f"Агента {agent!r} нет в этой сборке. Доступны: {', '.join(names)}"
        )

    with _lock:
        running = [
            bot for bot in _bots.values()
            if _alive(bot) or bot.get("switching")
        ]
        if len(running) >= MAX_BOTS:
            raise RuntimeError(f"Уже {MAX_BOTS} ботов. Остановите одного, чтобы посадить ещё.")
        key = username.casefold()
        # Soft-stopping still holds the Colyseus seat. Starting a second
        # process for the same login → OTHER_SESSION → connected=false →
        # site bot. Kill the old one first.
        stale = [
            bot for bot in _bots.values()
            if bot["login"].casefold() == key
            and (_alive(bot) or bot.get("switching") or bot.get("stopping"))
        ]
        for bot in stale:
            _push(bot, "[стол] новый старт того же логина — жёстко гашу старый процесс")
            _hard_stop(bot)

    # Direct login only — proxy/VPN removed from the desk.
    proxy = ""
    proxy_meta: dict = {"mode": "direct", "reason": "прокси отключены"}
    cookies = _login(username, password, proxy="")

    global _seq
    with _lock:
        key = username.casefold()
        if any(
            bot["login"].casefold() == key and (_alive(bot) or bot.get("switching"))
            for bot in _bots.values()
        ):
            raise RuntimeError("Этот логин уже сидит за столом. Остановите его, прежде чем сажать снова.")
        _seq += 1
        bot_id = f"{_seq:03d}"
        stop_file = RUNS / f"{bot_id}.stop"
        bot = {
            "id": bot_id,
            "login": username,
            "password": password,
            "variant": variant,
            "agent": agent,
            "once": once,
            "rejoinDelay": rejoin_delay,
            "maxRounds": max_rounds,
            "reconnect": reconnect and not once,
            "points": points,
            "tableMode": str(table_mode or "").strip(),
            "tableCreator": str(table_creator or "").strip(),
            "tablePassword": str(table_password or "").strip(),
            "startedAt": time.time(),
            "lines": [],
            "proc": None,
            "stopFile": str(stop_file),
            "stopping": False,
            "switching": False,
            "proxy": proxy,
            "proxyHost": proxy_meta.get("host") or "",
            "proxyRedacted": proxy_meta.get("redacted") or "",
        }
        _bots[bot_id] = bot
        mode = bot["tableMode"] or "create"
        if mode == "join" and bot["tableCreator"]:
            _push(bot, f"[стол] join → стол {bot['tableCreator']}"
                  + (" (пароль)" if bot["tablePassword"] else ""))
        elif bot["tablePassword"]:
            _push(bot, "[стол] create приватный стол (пароль)")
        else:
            _push(bot, "[стол] прямое подключение (без прокси)")
        _spawn_process(bot, cookies, agent)
    return {"id": bot_id, "warning": "", "proxy": proxy_meta}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, payload: dict | None = None, body: bytes | None = None,
              content_type: str = "application/json; charset=utf-8") -> None:
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body or b"")))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _bot(self, bot_id: str) -> dict | None:
        if not re.fullmatch(r"\d+", bot_id or ""):
            return None
        return _bots.get(bot_id)

    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k.strip()] = urllib.parse.unquote(v.strip())
        return out

    def _admin_ok(self) -> bool:
        # 127.0.0.1 ≠ замена админ-токена (ТЗ independent review §14).
        want = history_svc.admin_token()
        cookies = self._cookies()
        got = str(cookies.get("desk_admin") or "").strip()
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip() or got
        hdr = str(self.headers.get("X-Desk-Admin") or "").strip()
        if hdr:
            got = hdr
        if not want or not got:
            return False
        return secrets_compare(got, want)

    def _require_admin(self) -> bool:
        if self._admin_ok():
            return True
        self._send(401, {"error": "Требуется авторизация администратора."})
        return False

    def _send_admin_cookie(self, code: int, payload: dict, token: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"desk_admin={urllib.parse.quote(token)}; Path=/; HttpOnly; SameSite=Strict",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(path.query)
        parts = [part for part in path.path.split("/") if part]

        if parts == ["api", "admin", "status"]:
            self._send(200, {"ok": True, "admin": self._admin_ok()})
            return
        if parts == ["api", "history", "games"]:
            if not self._require_admin():
                return
            try:
                date_from = float((query.get("from") or [""])[0] or 0) or None
                date_to = float((query.get("to") or [""])[0] or 0) or None
                payload = history_svc.list_games(
                    bot_id=str((query.get("bot") or [""])[0] or ""),
                    status=str((query.get("status") or [""])[0] or ""),
                    date_from=date_from,
                    date_to=date_to,
                    packed=str((query.get("packed") or ["open"])[0] or "open"),
                    offset=int((query.get("offset") or ["0"])[0] or 0),
                    limit=int((query.get("limit") or ["50"])[0] or 50),
                )
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(200, payload)
            return
        if len(parts) == 4 and parts[:3] == ["api", "history", "games"]:
            if not self._require_admin():
                return
            try:
                payload = history_svc.load_game(
                    str((query.get("bot") or [""])[0] or ""),
                    parts[3],
                )
            except RuntimeError as exc:
                self._send(404, {"error": str(exc)})
                return
            self._send(200, payload)
            return
        if parts == ["api", "history", "packs"]:
            if not self._require_admin():
                return
            self._send(200, history_svc.list_packs(
                query=str((query.get("q") or [""])[0] or ""),
                date=str((query.get("date") or [""])[0] or ""),
                offset=int((query.get("offset") or ["0"])[0] or 0),
                limit=int((query.get("limit") or ["30"])[0] or 30),
            ))
            return
        if parts == ["api", "history", "compare"]:
            if not self._require_admin():
                return
            try:
                limit = int((query.get("limit") or ["80"])[0] or 80)
            except ValueError:
                limit = 80
            self._send(200, history_svc.compare_experience_agents(max_games=limit))
            return
        if parts == ["api", "history", "day"]:
            if not self._require_admin():
                return
            self._send(200, history_svc.bot_day_stats(
                day=str((query.get("day") or [""])[0] or ""),
                login=str((query.get("login") or [""])[0] or ""),
            ))
            return
        if len(parts) == 5 and parts[:3] == ["api", "history", "packs"] and parts[4] == "download":
            if not self._require_admin():
                return
            file_path = history_svc.pack_file(parts[3])
            if file_path is None:
                self._send(404, {"error": "Сборник не найден."})
                return
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{file_path.name}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
            return
        if len(parts) == 6 and parts[:3] == ["api", "history", "packs"] and parts[4] == "games":
            if not self._require_admin():
                return
            try:
                game = history_svc.load_pack_game(parts[3], parts[5])
            except RuntimeError as exc:
                self._send(404, {"error": str(exc)})
                return
            self._send(200, game)
            return
        if len(parts) == 4 and parts[:3] == ["api", "history", "jobs"]:
            if not self._require_admin():
                return
            try:
                self._send(200, history_svc.job_status(parts[3]))
            except RuntimeError as exc:
                self._send(404, {"error": str(exc)})
            return

        if parts == ["api", "belot-profiles"]:
            # Страница рейтинга снята — без login/home к belot.md.
            self._send(200, {"ok": True, "bots": [], "ttlSec": BELOT_PROFILE_TTL_S, "disabled": True})
            return
        if parts == ["api", "bots"]:
            with _lock:
                payload = [_public(bot) for bot in _bots.values()]
            payload.sort(key=lambda item: item["startedAt"], reverse=True)
            self._send(200, {"bots": payload})
            return
        if len(parts) == 3 and parts[:2] == ["api", "bots"]:
            bot = self._bot(parts[2])
            if bot is None:
                self._send(404, {"error": "Такого бота нет."})
                return
            after = int((query.get("after") or ["0"])[0] or 0)
            with _lock:
                self._send(200, _public(bot, with_lines=True, after=after))
            return
        if parts == ["api", "agents"]:
            variant = (query.get("variant") or ["3"])[0]
            if variant not in BUILDS:
                self._send(400, {"error": "Выберите стол на 3 или на 4."})
                return
            try:
                names = _agents(variant)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._send(500, {"error": f"Не удалось прочитать список ботов: {exc}"})
                return
            self._send(200, {
                "agents": names,
                "default": "strong" if "strong" in names else (names[0] if names else "random"),
                "build": str(BUILDS[variant]),
            })
            return
        if parts == ["api", "compare"]:
            self._send(200, _compare_agents())
            return
        if parts == ["api", "profiles"]:
            with _profiles_lock:
                rows = _load_profiles()
            # localhost-стол: пароли нужны UI, чтобы выбрать профиль и сесть
            self._send(200, {"profiles": [dict(row) for row in rows if isinstance(row, dict)]})
            return
        if parts == ["api", "proxies"]:
            self._send(410, {"error": "Раздел прокси удалён.", "enabled": False, "proxies": []})
            return

        rel = "index.html" if path.path in ("", "/") else path.path.lstrip("/")
        file_path = (STATIC / rel).resolve()
        if not str(file_path).startswith(str(STATIC.resolve())) or not file_path.is_file():
            self._send(404, {"error": "Нет такой страницы."})
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(file_path.suffix, "application/octet-stream")
        self._send(200, body=file_path.read_bytes(), content_type=kind)

    def do_POST(self) -> None:  # noqa: N802
        parts = [part for part in urllib.parse.urlparse(self.path).path.split("/") if part]
        data = self._read_json()
        try:
            if parts == ["api", "start"]:
                username = str(data.get("login") or "").strip()
                password = str(data.get("password") or "")
                if not username or not password:
                    raise RuntimeError("Введите логин и пароль.")
                agent = str(data.get("agent") or "strong")
                variant = str(data.get("variant") or "3")
                once = bool(data.get("once"))
                points = str(data.get("points") or "")
                result = _start(
                    variant,
                    agent,
                    username,
                    password,
                    once,
                    rejoin_delay=float(data.get("rejoinDelay") or 5),
                    max_rounds=int(data.get("maxRounds") or 0),
                    reconnect=bool(data.get("reconnect", True)),
                    points=points,
                    table_mode=str(data.get("tableMode") or ""),
                    table_creator=str(data.get("tableCreator") or ""),
                    table_password=str(data.get("tablePassword") or ""),
                )
                if data.get("saveProfile", True):
                    try:
                        _upsert_profile(
                            username, password,
                            agent=agent, variant=variant,
                            points=points, once=once,
                        )
                    except RuntimeError:
                        pass
                self._send(200, {"ok": True, **result})
                return
            if parts == ["api", "train", "start"]:
                # Private 3p self-play: seat 0 creates password table, 1–2 join.
                seats = data.get("seats") or []
                if not isinstance(seats, list) or len(seats) != 3:
                    raise RuntimeError("Нужно ровно 3 профиля: хост и два гостя.")
                logins = []
                for i, seat in enumerate(seats):
                    if not isinstance(seat, dict):
                        raise RuntimeError(f"Место {i + 1}: укажите профиль.")
                    login = str(seat.get("login") or "").strip()
                    if not login:
                        raise RuntimeError(f"Место {i + 1}: выберите профиль.")
                    logins.append(login)
                if len({x.casefold() for x in logins}) != 3:
                    raise RuntimeError("Три разных профиля — нельзя одного дважды.")
                by = {str(p.get("login") or "").casefold(): p for p in _load_profiles()}
                for login in logins:
                    if login.casefold() not in by or not by[login.casefold()].get("password"):
                        raise RuntimeError(f"Нет сохранённого пароля для {login}.")
                agent = str(data.get("agent") or "fallen3").strip() or "fallen3"
                points = str(data.get("points") or "101").strip() or "101"
                if points not in ("51", "101"):
                    points = "101"
                table_password = str(data.get("tablePassword") or "").strip()
                if not table_password:
                    table_password = "trio" + secrets.token_hex(3)
                host = logins[0]
                started = []
                # Host first, then guests with a short gap.
                for i, login in enumerate(logins):
                    prof = by[login.casefold()]
                    mode = "create" if i == 0 else "join"
                    result = _start(
                        "3",
                        agent,
                        login,
                        str(prof.get("password") or ""),
                        False,
                        rejoin_delay=5.0,
                        max_rounds=0,
                        reconnect=True,
                        points=points,
                        table_mode=mode,
                        table_creator="" if i == 0 else host,
                        table_password=table_password,
                    )
                    started.append({
                        "login": login,
                        "role": "host" if i == 0 else "guest",
                        "id": result.get("id"),
                        "tableMode": mode,
                    })
                    if i + 1 < len(logins):
                        time.sleep(RESTORE_GAP_S)
                self._send(200, {
                    "ok": True,
                    "tablePassword": table_password,
                    "host": host,
                    "agent": agent,
                    "points": points,
                    "started": started,
                })
                return
            if parts == ["api", "train", "stop"]:
                logins = data.get("logins") or []
                if not isinstance(logins, list) or not logins:
                    raise RuntimeError("Укажите логины трио для остановки.")
                want = {str(x or "").strip().casefold() for x in logins if str(x or "").strip()}
                stopped = []
                with _lock:
                    bots = list(_bots.values())
                for bot in bots:
                    if bot.get("login", "").casefold() not in want:
                        continue
                    with _lock:
                        if _soft_stop(bot):
                            stopped.append(bot.get("login"))
                self._send(200, {"ok": True, "stopped": stopped})
                return
            if parts == ["api", "profiles"]:
                saved = _upsert_profile(
                    str(data.get("login") or ""),
                    str(data.get("password") or ""),
                    agent=str(data.get("agent") or "strong"),
                    variant=str(data.get("variant") or "3"),
                    points=str(data.get("points") or ""),
                    once=bool(data.get("once", False)),
                )
                self._send(200, {"ok": True, "profile": saved})
                return
            if parts == ["api", "proxies"] or parts == ["api", "proxies", "probe"]:
                self._send(410, {"error": "Раздел прокси удалён.", "enabled": False})
                return
            if parts == ["api", "profiles", "delete"]:
                ok = _delete_profile(str(data.get("login") or ""))
                if not ok:
                    self._send(404, {"error": "Такого профиля нет."})
                    return
                self._send(200, {"ok": True})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "stop":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                with _lock:
                    ok = _soft_stop(bot)
                    remaining = _remaining(bot)
                if not ok:
                    self._send(400, {"error": "Этот бот уже не за столом."})
                    return
                self._send(200, {"ok": True, "stopping": True, "remaining": remaining})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "continue":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                with _lock:
                    ok = _cancel_stop(bot)
                if not ok:
                    self._send(400, {"error": "Этот бот уже не за столом."})
                    return
                self._send(200, {"ok": True, "stopping": False})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "switch-agent":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                agent = str(data.get("agent") or "").strip()
                if not agent:
                    self._send(400, {"error": "Укажите агента."})
                    return
                with _lock:
                    result = _switch_agent(bot, agent)
                self._send(200, {"ok": True, **result})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "cancel-switch":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                with _lock:
                    ok = _cancel_switch(bot)
                if not ok:
                    self._send(400, {"error": "Смена агента сейчас не идёт."})
                    return
                self._send(200, {"ok": True, "switching": False})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "clear":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                with _lock:
                    bot["lines"].clear()
                self._send(200, {"ok": True})
                return
            if len(parts) == 4 and parts[:2] == ["api", "bots"] and parts[3] == "mark":
                bot = self._bot(parts[2])
                if bot is None:
                    self._send(404, {"error": "Такого бота нет."})
                    return
                about = data.get("aboutTrick") or data.get("about_trick")
                with _lock:
                    result = _save_mark(
                        bot,
                        note=str(data.get("note") or ""),
                        better=str(data.get("better") or ""),
                        better_card=str(data.get("betterCard") or data.get("better_card") or ""),
                        about_trick=about if isinstance(about, dict) else None,
                    )
                self._send(200, result)
                return
            if parts == ["api", "stop-all"]:
                with _lock:
                    bots = list(_bots.values())
                for bot in bots:
                    with _lock:
                        _soft_stop(bot)
                with _session_lock:
                    _save_session([])
                self._send(200, {"ok": True})
                return
            if parts == ["api", "belot-profiles", "refresh"]:
                self._send(410, {"error": "Рейтинг belot.md отключён (бан IP).", "disabled": True})
                return
            if parts == ["api", "belot-profiles", "push"]:
                self._send(200, {"ok": True, "disabled": True, "profile": None})
                return
            if parts == ["api", "admin", "login"]:
                token = str(data.get("token") or "").strip()
                want = history_svc.admin_token()
                if not token or not secrets_compare(token, want):
                    self._send(401, {"error": "Неверный токен администратора."})
                    return
                self._send_admin_cookie(200, {"ok": True, "admin": True}, want)
                return
            if parts == ["api", "admin", "logout"]:
                body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie", "desk_admin=; Path=/; Max-Age=0; SameSite=Strict")
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["api", "history", "pack"]:
                if not self._require_admin():
                    return
                ids = data.get("gameIds") or data.get("games") or []
                if not isinstance(ids, list):
                    raise RuntimeError("Передайте gameIds: [...].")
                job = history_svc.start_pack_job(
                    [str(x) for x in ids],
                    idempotency_key=str(data.get("idempotencyKey") or data.get("idempotency") or ""),
                )
                self._send(200, {"ok": True, "job": job})
                return
            if parts == ["api", "history", "rebuild-index"]:
                if not self._require_admin():
                    return
                payload = history_svc.rebuild_index()
                self._send(200, {"ok": True, "total": len(payload.get("games") or {})})
                return
            if len(parts) == 5 and parts[:3] == ["api", "history", "packs"] and parts[4] == "cleanup":
                if not self._require_admin():
                    return
                row = history_svc.retry_cleanup(parts[3])
                self._send(200, {"ok": True, "pack": row})
                return
            if parts == ["api", "history", "review-mark"]:
                if not self._require_admin():
                    return
                mark = history_svc.save_owner_review_mark(data if isinstance(data, dict) else {})
                self._send(200, {"ok": True, "mark": mark})
                return
        except RuntimeError as exc:
            payload = {"error": str(exc)}
            proxy_meta = getattr(exc, "proxy", None)
            if isinstance(proxy_meta, dict) and proxy_meta.get("host"):
                payload["proxy"] = proxy_meta
            self._send(400, payload)
            return
        self._send(404, {"error": "Нет такого действия."})


def secrets_compare(a: str, b: str) -> bool:
    import secrets
    return bool(a) and bool(b) and secrets.compare_digest(a, b)


def main() -> None:
    RUNS.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    history_svc.ensure_dirs()
    history_svc.rebuild_index()
    history_svc.recover_jobs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Stol belotmd_agent: http://{HOST}:{PORT}", flush=True)
    print(f"  3p -> {BUILDS['3']}", flush=True)
    print(f"  4p -> {BUILDS['4']}", flush=True)
    print(f"  history -> {HISTORY_DIR}", flush=True)
    threading.Thread(target=_restore_session_bg, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Не чистим session.json — после обновления стола боты сядут сами.
        with _lock:
            bots = list(_bots.values())
        for bot in bots:
            _hard_stop(bot)
        server.shutdown()


if __name__ == "__main__":
    main()
