"""История игр и ручная сборка опыта для панели desk.

Каталог: desk/history/
  bots/<bot_id>/game_*.jsonl + games.json
  packs/опыт_DD.MM.YYYY_NNN.json + registry.json
  jobs/<job_id>.json
  index/games.json  (сводный индекс)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HISTORY_ROOT = Path(os.environ.get("BELOT_HISTORY_DIR") or (ROOT / "history"))
BOTS_DIR = HISTORY_ROOT / "bots"
PACKS_DIR = HISTORY_ROOT / "packs"
JOBS_DIR = HISTORY_ROOT / "jobs"
INDEX_DIR = HISTORY_ROOT / "index"
ADMIN_FILE = ROOT / "admin.token"
REGISTRY_FILE = PACKS_DIR / "registry.json"
INDEX_FILE = INDEX_DIR / "games.json"

PACK_FORMAT = "2"
PACK_HANDLER = "belotmd-experience-2"
TZ_NAME = os.environ.get("BELOT_DESK_TZ", "Europe/Chisinau")
MAX_JOBS = 2
# ТЗ §1/§9: до безопасной очистки авто-удаление исходников выключено.
# Включить только явно: BELOT_HISTORY_AUTO_CLEANUP=1
AUTO_CLEANUP = os.environ.get("BELOT_HISTORY_AUTO_CLEANUP", "0").strip() == "1"

_lock = threading.RLock()
_jobs_running = 0
_pack_day_lock = threading.Lock()

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _now_local() -> datetime:
    """Локальное время панели; ZoneInfo опционален (на Windows нужен tzdata)."""
    try:
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now().astimezone()


def ensure_dirs() -> None:
    for path in (BOTS_DIR, PACKS_DIR, JOBS_DIR, INDEX_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl_tolerant(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def read_jsonl_strict(path: Path) -> tuple[list[dict], list[dict]]:
    """Чтение JSONL с отчётом о битых строках (для cleanup / сверки)."""
    if not path.is_file():
        return [], [{"file": str(path), "error": "missing"}]
    rows: list[dict] = []
    errors: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append({"file": str(path), "line": i, "error": str(exc)})
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            errors.append({"file": str(path), "line": i, "error": "not_object"})
    return rows, errors


# --- admin -----------------------------------------------------------------

def admin_token() -> str:
    env = str(os.environ.get("BELOT_DESK_ADMIN_TOKEN") or "").strip()
    if env:
        return env
    ensure_dirs()
    if ADMIN_FILE.is_file():
        token = ADMIN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    ADMIN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(ADMIN_FILE, 0o600)
    except OSError:
        pass
    return token


def check_admin(header_token: str | None, cookie_token: str | None = None) -> bool:
    want = admin_token()
    got = (header_token or cookie_token or "").strip()
    return bool(got) and secrets.compare_digest(got, want)


# --- index -----------------------------------------------------------------

def rebuild_index() -> dict:
    ensure_dirs()
    games = {}
    if BOTS_DIR.is_dir():
        for bot_dir in BOTS_DIR.iterdir():
            if not bot_dir.is_dir():
                continue
            local = _read_json(bot_dir / "games.json", {})
            local_games = local.get("games") if isinstance(local, dict) else {}
            if isinstance(local_games, dict):
                for gid, meta in local_games.items():
                    if isinstance(meta, dict) and gid:
                        games[str(gid)] = meta
            # Файлы без индекса — восстановить мета из header/final
            for path in bot_dir.glob("game_*.jsonl"):
                gid = path.stem.replace("game_", "", 1)
                if gid in games:
                    continue
                rows = read_jsonl_tolerant(path)
                if not rows:
                    continue
                meta = _meta_from_rows(rows, bot_dir.name, path)
                games[meta["gameId"]] = meta
    payload = {"games": games, "updatedAt": time.time()}
    _atomic_json(INDEX_FILE, payload)
    # закрыть брошенные running после пересборки
    try:
        reconcile_stale_games()
        data2 = _read_json(INDEX_FILE, payload)
        if isinstance(data2, dict) and isinstance(data2.get("games"), dict):
            return data2
    except Exception:
        pass
    return payload


def _meta_from_rows(rows: list[dict], bot_id: str, path: Path) -> dict:
    header = next((r for r in rows if r.get("type") == "meta"), rows[0])
    final = next((r for r in reversed(rows) if r.get("type") == "final"), None)
    gid = str(header.get("gameId") or path.stem.replace("game_", "", 1))
    status = "incomplete"
    if final:
        status = str(final.get("status") or "incomplete")
    elif header.get("status") == "running":
        status = "running"
    return {
        "gameId": gid,
        "botId": bot_id,
        "login": header.get("login"),
        "file": path.name,
        "status": status,
        "agent": header.get("agent"),
        "agentVersion": header.get("agentVersion"),
        "modelVersion": header.get("modelVersion"),
        "variant": header.get("variant"),
        "points": header.get("points"),
        "startedAt": header.get("startedAt") or header.get("t"),
        "endedAt": (final or {}).get("endedAt"),
        "hands": (final or {}).get("hands") or sum(1 for r in rows if r.get("type") == "hand_start"),
        "decisions": (final or {}).get("decisions") or sum(1 for r in rows if r.get("type") == "decision"),
        "result": (final or {}).get("result"),
        "scores": (final or {}).get("scores") or [],
        "matchKey": header.get("matchKey"),
        "seat": header.get("seat"),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "packedIn": None,
        "quality": "ok" if status == "completed" else "incomplete",
    }


def upsert_game_meta(meta: dict) -> None:
    ensure_dirs()
    with _lock:
        data = _read_json(INDEX_FILE, {"games": {}})
        games = data.get("games") if isinstance(data, dict) else {}
        if not isinstance(games, dict):
            games = {}
        gid = str(meta.get("gameId") or "")
        if not gid:
            return
        prev = games.get(gid) if isinstance(games.get(gid), dict) else {}
        merged = dict(prev)
        merged.update({k: v for k, v in meta.items() if v is not None})
        games[gid] = merged
        _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})


def _day_bounds(day: str = "") -> tuple[float, float, str]:
    """Границы календарного дня панели. day: YYYY-MM-DD или пусто = сегодня."""
    now = _now_local()
    if day:
        try:
            y, m, d = (int(x) for x in str(day).strip().split("-", 2))
            start_dt = now.replace(year=y, month=m, day=d, hour=0, minute=0, second=0, microsecond=0)
        except (TypeError, ValueError):
            start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_dt.timestamp()
    end = start + 86400.0
    return start, end, start_dt.strftime("%Y-%m-%d")


def bot_day_stats(*, day: str = "", login: str = "") -> dict:
    """Сводка по ботам за день: игр / побед / поражений / раздач."""
    ensure_dirs()
    start, end, day_key = _day_bounds(day)
    data = _read_json(INDEX_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
        data = rebuild_index()
    want = (login or "").strip().casefold()
    buckets: dict[str, dict] = {}
    for row in (data.get("games") or {}).values():
        if not isinstance(row, dict):
            continue
        started = float(row.get("startedAt") or 0)
        if started < start or started >= end:
            continue
        name = str(row.get("login") or row.get("botId") or "?").strip() or "?"
        if want and name.casefold() != want and str(row.get("botId") or "").casefold() != want:
            continue
        key = name.casefold()
        slot = buckets.get(key)
        if slot is None:
            slot = {
                "login": name,
                "botId": str(row.get("botId") or ""),
                "agent": str(row.get("agent") or ""),
                "games": 0,
                "completed": 0,
                "running": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "hands": 0,
                "recent": [],
            }
            buckets[key] = slot
        if row.get("agent") and not slot["agent"]:
            slot["agent"] = str(row.get("agent"))
        if row.get("botId"):
            slot["botId"] = str(row.get("botId"))
        status = str(row.get("status") or "")
        result = str(row.get("result") or "").strip().lower()
        hands = int(row.get("hands") or 0)
        slot["games"] += 1
        slot["hands"] += hands
        if status == "running":
            slot["running"] += 1
        elif status in ("completed", "packed"):
            slot["completed"] += 1
            if result in ("победа", "win", "won"):
                slot["wins"] += 1
            elif result in ("поражение", "loss", "lost"):
                slot["losses"] += 1
            else:
                slot["draws"] += 1
        slot["recent"].append({
            "gameId": row.get("gameId"),
            "startedAt": started,
            "status": status,
            "result": row.get("result"),
            "hands": hands,
            "agent": row.get("agent"),
            "file": row.get("file"),
            "bytes": row.get("bytes"),
        })
    bots = []
    for slot in buckets.values():
        finished = slot["wins"] + slot["losses"]
        slot["winRate"] = round(100.0 * slot["wins"] / finished, 1) if finished else None
        slot["recent"].sort(key=lambda r: float(r.get("startedAt") or 0), reverse=True)
        # агент с самой свежей игры (не первый попавшийся fallen)
        counts: dict[str, int] = {}
        for g in slot["recent"]:
            a = str(g.get("agent") or "").strip()
            if a:
                counts[a] = counts.get(a, 0) + 1
        latest = next((str(g.get("agent") or "") for g in slot["recent"] if g.get("agent")), "")
        # если сейчас running — приоритет агенту из самой свежей running
        live_agent = next(
            (str(g.get("agent") or "") for g in slot["recent"] if g.get("status") == "running" and g.get("agent")),
            "",
        )
        slot["agent"] = live_agent or latest or slot.get("agent") or ""
        slot["agents"] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if not want:
            slot["recent"] = slot["recent"][:8]
        bots.append(slot)
    bots.sort(key=lambda r: (-int(r["games"]), str(r["login"]).casefold()))
    return {
        "day": day_key,
        "dayStart": start,
        "dayEnd": end,
        "tz": TZ_NAME,
        "bots": bots,
        "totals": {
            "bots": len(bots),
            "games": sum(b["games"] for b in bots),
            "wins": sum(b["wins"] for b in bots),
            "losses": sum(b["losses"] for b in bots),
            "hands": sum(b["hands"] for b in bots),
        },
    }



def reconcile_stale_games() -> dict:
    """Закрыть брошенные running: у бота только самая свежая остаётся running.
    Остальные → interrupted (не показываем в открытом списке).
    Если в jsonl уже есть final — подтянуть статус из файла.
    """
    ensure_dirs()
    closed = 0
    refreshed = 0
    with _lock:
        data = _read_json(INDEX_FILE, {"games": {}})
        games = data.get("games") if isinstance(data, dict) else {}
        if not isinstance(games, dict):
            games = {}
        # 1) обновить running из файла (вдруг уже есть final)
        for gid, meta in list(games.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("packedIn"):
                continue
            if str(meta.get("status") or "") != "running":
                continue
            bot_id = str(meta.get("botId") or "")
            path = game_path(bot_id, str(gid))
            if path is None or not path.is_file():
                continue
            rows = read_jsonl_tolerant(path)
            if not rows:
                continue
            fresh = _meta_from_rows(rows, bot_id, path)
            if fresh.get("status") and fresh["status"] != "running":
                merged = dict(meta)
                merged.update({k: v for k, v in fresh.items() if v is not None})
                # сохранить packedIn если был
                merged["packedIn"] = meta.get("packedIn")
                games[gid] = merged
                refreshed += 1
                local_path = BOTS_DIR / bot_id / "games.json"
                local = _read_json(local_path, {"games": {}})
                if isinstance(local.get("games"), dict) and gid in local["games"]:
                    local["games"][gid].update({k: merged[k] for k in ("status", "endedAt", "result", "hands", "decisions", "scores", "quality") if k in merged})
                    _atomic_json(local_path, local)
        # 2) running только у самой свежей игры бота; всё старше — interrupted
        by_bot: dict[str, list[tuple[str, dict]]] = {}
        for gid, meta in games.items():
            if not isinstance(meta, dict) or meta.get("packedIn"):
                continue
            bot_id = str(meta.get("botId") or "") or "?"
            by_bot.setdefault(bot_id, []).append((str(gid), meta))
        now = time.time()
        for bot_id, items in by_bot.items():
            items.sort(key=lambda it: float(it[1].get("startedAt") or 0), reverse=True)
            latest_gid = items[0][0] if items else ""
            for gid, meta in items:
                if str(meta.get("status") or "") != "running":
                    continue
                if gid == latest_gid:
                    continue
                meta = dict(meta)
                meta["status"] = "interrupted"
                meta["endedAt"] = meta.get("endedAt") or now
                meta["quality"] = "incomplete"
                if not meta.get("result"):
                    meta["result"] = None
                games[gid] = meta
                closed += 1
                local_path = BOTS_DIR / bot_id / "games.json"
                local = _read_json(local_path, {"games": {}})
                if isinstance(local.get("games"), dict) and gid in local["games"]:
                    local["games"][gid]["status"] = "interrupted"
                    local["games"][gid]["endedAt"] = meta["endedAt"]
                    local["games"][gid]["quality"] = "incomplete"
                    _atomic_json(local_path, local)
        _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
    return {"closed": closed, "refreshed": refreshed}


def list_games(
    *,
    bot_id: str = "",
    status: str = "",
    date_from: float | None = None,
    date_to: float | None = None,
    packed: str = "open",  # open | packed | all
    offset: int = 0,
    limit: int = 50,
) -> dict:
    ensure_dirs()
    data = _read_json(INDEX_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
        data = rebuild_index()
    rows = list((data.get("games") or {}).values())
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if bot_id and str(row.get("botId")) != bot_id and str(row.get("login") or "").casefold() != bot_id.casefold():
            continue
        if status and str(row.get("status")) != status:
            continue
        started = float(row.get("startedAt") or 0)
        if date_from is not None and started < date_from:
            continue
        if date_to is not None and started > date_to:
            continue
        packed_in = row.get("packedIn")
        if packed == "open" and packed_in:
            continue
        if packed == "packed" and not packed_in:
            continue
        # в открытом списке — только текущие running и готовые completed
        if packed == "open":
            st = str(row.get("status") or "")
            if st not in ("running", "completed", "processing"):
                continue
        out.append(row)
    out.sort(key=lambda r: float(r.get("startedAt") or 0), reverse=True)
    total = len(out)
    page = out[offset: offset + max(1, min(limit, 200))]
    bots = sorted({str(r.get("botId")) for r in out if r.get("botId")})
    return {"total": total, "offset": offset, "limit": limit, "games": page, "bots": bots}


def game_path(bot_id: str, game_id: str) -> Path | None:
    if not SAFE_ID.match(bot_id or "") or not SAFE_ID.match(game_id or ""):
        return None
    path = BOTS_DIR / bot_id / f"game_{game_id}.jsonl"
    if path.is_file():
        return path
    # fallback scan
    data = _read_json(INDEX_FILE, {"games": {}})
    meta = (data.get("games") or {}).get(game_id)
    if isinstance(meta, dict) and meta.get("file"):
        cand = BOTS_DIR / str(meta.get("botId") or bot_id) / str(meta["file"])
        if cand.is_file():
            return cand
    return None


def load_game(bot_id: str, game_id: str) -> dict:
    path = game_path(bot_id, game_id)
    if path is None:
        # try index only bot id
        data = _read_json(INDEX_FILE, {"games": {}})
        meta = (data.get("games") or {}).get(game_id)
        if not isinstance(meta, dict):
            raise RuntimeError("Игра не найдена.")
        path = game_path(str(meta.get("botId") or ""), game_id)
        if path is None:
            raise RuntimeError("Файл игры не найден.")
        bot_id = str(meta.get("botId") or bot_id)
    rows = read_jsonl_tolerant(path)
    meta = _meta_from_rows(rows, bot_id, path)
    recon, decisions, bits = [], [], {}
    try:
        recon, decisions, bits = _run_game_analysis(rows)
        meta = {**meta, "reconstruction": bits}
    except Exception as exc:
        bits = {"error": str(exc)}
    return {
        "meta": meta,
        "events": rows,
        "path": str(path),
        "reconstructed_hands": recon,
        "decisions": decisions,
        "analysis": bits,
    }


# --- packs / jobs ----------------------------------------------------------

def _registry() -> dict:
    data = _read_json(REGISTRY_FILE, {"packs": [], "daySeq": {}})
    if not isinstance(data, dict):
        data = {"packs": [], "daySeq": {}}
    data.setdefault("packs", [])
    data.setdefault("daySeq", {})
    return data


def _save_registry(data: dict) -> None:
    _atomic_json(REGISTRY_FILE, data)


def _next_pack_name() -> tuple[str, str, int, str]:
    """Атомарно: имя файла, дата, номер дня, pack_id."""
    with _pack_day_lock:
        now = _now_local()
        day = now.strftime("%d.%m.%Y")
        reg = _registry()
        day_seq = reg.setdefault("daySeq", {})
        n = int(day_seq.get(day) or 0) + 1
        day_seq[day] = n
        pack_id = f"pack_{now.strftime('%Y%m%d')}_{n:03d}_{uuid.uuid4().hex[:8]}"
        filename = f"опыт_{day}_{n:03d}.json"
        _save_registry(reg)
        return filename, day, n, pack_id


def list_packs(*, query: str = "", date: str = "", offset: int = 0, limit: int = 30) -> dict:
    ensure_dirs()
    packs = list(_registry().get("packs") or [])
    q = query.casefold()
    out = []
    for row in packs:
        if not isinstance(row, dict):
            continue
        if date and str(row.get("day") or "") != date and not str(row.get("filename") or "").startswith(f"опыт_{date}_"):
            continue
        blob = json.dumps(row, ensure_ascii=False).casefold()
        if q and q not in blob:
            continue
        out.append(row)
    out.sort(key=lambda r: float(r.get("createdAt") or 0), reverse=True)
    total = len(out)
    return {"total": total, "offset": offset, "limit": limit, "packs": out[offset: offset + max(1, min(limit, 100))]}


def pack_file(pack_id: str) -> Path | None:
    if not SAFE_ID.match(pack_id or ""):
        # allow Unicode filenames via registry only
        pass
    for row in _registry().get("packs") or []:
        if not isinstance(row, dict):
            continue
        if row.get("packId") == pack_id or row.get("filename") == pack_id:
            path = PACKS_DIR / str(row.get("filename"))
            if path.is_file() and path.resolve().is_relative_to(PACKS_DIR.resolve()):
                return path
    return None


def load_pack_game(pack_id: str, game_id: str) -> dict:
    path = pack_file(pack_id)
    if path is None:
        raise RuntimeError("Сборник не найден.")
    data = json.loads(path.read_text(encoding="utf-8"))
    found = None
    for game in data.get("games") or []:
        if str(game.get("gameId")) == game_id or str((game.get("meta") or {}).get("gameId")) == game_id:
            found = dict(game)
            break
    if found is None:
        raise RuntimeError("Игра в сборнике не найдена.")
    found["reconstructed_hands"] = [
        r for r in (data.get("reconstructed_hands") or [])
        if str(r.get("gameId")) == str(game_id)
    ]
    found["decisions"] = [
        d for d in (data.get("decisions") or [])
        if str(d.get("gameId")) == str(game_id)
    ]
    return found


def _situation_key(rec: dict) -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from belotmd.history.keys import situation_key
        return situation_key(rec)
    except Exception:
        import hashlib
        raw = json.dumps({
            "phase": rec.get("phase"),
            "trump": (rec.get("state") or {}).get("trump") if isinstance(rec.get("state"), dict) else rec.get("trump"),
            "seat": rec.get("seat"),
            "legal": rec.get("legalActions") or rec.get("legal_actions"),
            "hand": ((rec.get("state") or {}).get("hands") or [None])[int(rec.get("seat") or 0)]
            if isinstance(rec.get("state"), dict) else None,
            "trick": (rec.get("state") or {}).get("current_trick") if isinstance(rec.get("state"), dict) else None,
        }, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _run_game_analysis(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Реконструкция рук + двойная оценка. Возвращает (recon, decisions, quality_bits)."""
    src = Path(__file__).resolve().parent.parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from belotmd.history.reconstruct import reconstruct_game
    from belotmd.history.analyze import analyze_game, ANALYZER_VERSION
    recon = reconstruct_game(rows)
    decisions = analyze_game(rows, recon)
    bits = {
        "analyzer": ANALYZER_VERSION,
        "handsComplete": sum(1 for r in recon if r.get("status") == "complete"),
        "handsPartial": sum(1 for r in recon if r.get("status") == "partial"),
        "handsInvalid": sum(1 for r in recon if r.get("status") == "invalid"),
    }
    return recon, decisions, bits


def _build_pack(job: dict) -> dict:
    game_ids = list(job.get("gameIds") or [])
    index = _read_json(INDEX_FILE, {"games": {}})
    games_meta = index.get("games") or {}
    games_out = []
    reconstructed_all = []
    decisions_all = []
    situations: dict[str, dict] = {}
    quality = {
        "incomplete": [],
        "excluded": [],
        "warnings": [],
        "reconstruction": [],
        "analysisLimits": [
            "оценка at_time не использует скрытые руки и будущие ходы",
            "оценка post_reveal — для разбора, не для обучения как 'правильный ответ'",
            "выигрыш взятки ≠ автоматически удачный ход",
            "совпадение с правилом Fallen ≠ доказательство лучшего хода",
        ],
    }
    decision_count = 0
    bot_ids = set()
    match_keys = set()
    times = []
    seen_match_decision = set()  # (gameId/participation, decisionId) — не matchKey комнаты

    for i, gid in enumerate(game_ids):
        job["progress"] = {
            "checked": i,
            "processed": i,
            "left": len(game_ids) - i,
            "situations": len(situations),
            "errors": list(job.get("errors") or []),
        }
        _atomic_json(JOBS_DIR / f"{job['jobId']}.json", job)
        meta = games_meta.get(gid) if isinstance(games_meta.get(gid), dict) else None
        if not meta:
            job.setdefault("errors", []).append({"gameId": gid, "error": "нет в индексе"})
            continue
        if str(meta.get("status")) not in ("completed", "processing"):
            quality["excluded"].append({"gameId": gid, "reason": "не завершена"})
            continue
        if meta.get("packedIn"):
            quality["excluded"].append({"gameId": gid, "reason": "уже в сборнике"})
            continue
        path = game_path(str(meta.get("botId") or ""), gid)
        if path is None:
            job.setdefault("errors", []).append({"gameId": gid, "error": "файл отсутствует"})
            continue
        rows = read_jsonl_tolerant(path)
        if not rows:
            quality["incomplete"].append({"gameId": gid, "reason": "пустой файл"})
            continue
        gmeta = _meta_from_rows(rows, str(meta.get("botId")), path)
        bot_ids.add(gmeta["botId"])
        mk = gmeta.get("matchKey")
        if mk:
            match_keys.add(mk)
        if gmeta.get("startedAt"):
            times.append(float(gmeta["startedAt"]))
        if gmeta.get("endedAt"):
            times.append(float(gmeta["endedAt"]))

        try:
            recon, decisions, bits = _run_game_analysis(rows)
        except Exception as exc:
            quality["warnings"].append({"gameId": gid, "error": f"анализ: {exc}"})
            recon, decisions, bits = [], [], {}
        for r in recon:
            if r.get("status") != "complete":
                quality["reconstruction"].append({
                    "gameId": gid,
                    "handId": r.get("handId"),
                    "status": r.get("status"),
                    "reasons": r.get("contradictions") or [],
                })
            reconstructed_all.append({"gameId": gid, **r})

        for d in decisions:
            did = d.get("decisionId")
            # комната (matchKey) переиспользуется — дедуп только внутри участия bot×game
            dedupe = (gid, did)
            if did and dedupe in seen_match_decision:
                continue
            if did:
                seen_match_decision.add(dedupe)
            decisions_all.append({"gameId": gid, **d})
            decision_count += 1
            # situation key from post-reveal context where possible
            n_players = (
                int(gmeta.get("variant") or 3)
                if str(gmeta.get("variant") or "3").isdigit()
                else 3
            )
            seat_i = d.get("seat")
            try:
                seat_i = int(seat_i) if seat_i is not None else None
            except (TypeError, ValueError):
                seat_i = None
            # руки: слоты 0..n-1, карта только на своём месте (не терять seat 1/2)
            hands_slots: list = [{"known": False, "count": 0} for _ in range(n_players)]
            hand_before = None
            trump_val = None
            declarer_val = None
            for rh in recon:
                if str(rh.get("handId")) != str(d.get("handId")):
                    continue
                if seat_i is None:
                    break
                seat_rec = (rh.get("seats") or {}).get(str(seat_i)) or {}
                # рука непосредственно перед этим ходом
                for step in seat_rec.get("handBeforePlays") or []:
                    if step.get("cardPlayed") == d.get("card") and (
                        step.get("trickNo") == d.get("trickNo") or d.get("trickNo") is None
                    ):
                        hand_before = list(step.get("handBefore") or [])
                        break
                if hand_before is None and seat_rec.get("status") == "complete":
                    hand_before = list(seat_rec.get("initialHand") or [])
                if hand_before is not None and 0 <= seat_i < n_players:
                    hands_slots[seat_i] = {"known": True, "cards": hand_before}
                break
            # козырь из событий раздачи
            for ev in rows:
                if ev.get("type") in ("hand_start", "trump") and str(ev.get("handId")) == str(d.get("handId")):
                    if ev.get("trump") is not None:
                        trump_val = ev.get("trump")
                    if ev.get("declarer") is not None:
                        declarer_val = ev.get("declarer")
            legal = (d.get("evalPostReveal") or {}).get("legalUsed") or d.get("legalActions") or []
            # неполные ситуации не сливаем с полными: помечаем incompleteness в ключе
            incomplete = hand_before is None
            # не подставлять [] вместо неизвестных graveyard/combinations/scores
            st_payload = {
                "phase": "PLAY",
                "trump": trump_val,
                "declarer": declarer_val,
                "current_trick": [
                    [x["seat"], x["card"]] for x in (d.get("tableBefore") or [])
                ],
                "hands": hands_slots,
                "num_players": n_players,
                "tricks_played": d.get("trickNo"),
            }
            if d.get("graveyard") is not None:
                st_payload["graveyard"] = d.get("graveyard")
            if d.get("combinations") is not None:
                st_payload["combinations"] = d.get("combinations")
            if "trumpOpen" in d or d.get("evalAtTime", {}).get("trumpOpenKnown"):
                st_payload["trumpOpen"] = d.get("trumpOpen")
            key_src = {
                "phase": "PLAY",
                "seat": seat_i,
                "game_mode": f"{gmeta.get('variant') or '3'}p",
                "trump": trump_val,
                "legal_actions": legal if legal else None,
                "legal_trusted": (d.get("evalPostReveal") or {}).get("legalTrusted"),
                "state": st_payload,
                "gameId": gid,
                "match_id": gid,  # не matchKey комнаты — комната переиспользуется
                "participation_id": gid,
                "incomplete": incomplete,
            }
            if d.get("matchScoresBefore") is not None:
                key_src["match_scores_before"] = d.get("matchScoresBefore")
            if incomplete:
                # отдельный ключ — не объединять с полными
                sk = f"incomplete-{gid}-{d.get('decisionId')}"
            else:
                try:
                    sk = _situation_key(key_src)
                except Exception:
                    sk = f"fallback-{d.get('seat')}-{d.get('card')}"
            bucket = situations.setdefault(sk, {
                "key": sk,
                "keyVersion": "exact-2",
                "grouping": "exact",
                "actions": {},
                "observations": 0,
                "games": set(),
                "refs": [],
            })
            action_key = str(d.get("card") if d.get("card") is not None else d.get("chosenAction"))
            act = bucket["actions"].setdefault(action_key, {
                "action": d.get("card") if d.get("card") is not None else d.get("chosenAction"),
                "count": 0,
                "wins": 0,
                "losses": 0,
                "pointsSum": 0,
                "labelsAtTime": {},
                "labelsPostReveal": {},
            })
            act["count"] += 1
            if d.get("evalPostReveal", {}).get("fact", {}).get("wonTrick") is True:
                act["wins"] += 1
            elif d.get("evalPostReveal", {}).get("fact", {}).get("wonTrick") is False:
                act["losses"] += 1
            pts = d.get("evalPostReveal", {}).get("fact", {}).get("trickPoints")
            if isinstance(pts, (int, float)):
                act["pointsSum"] += int(pts)
            for lab_key, lab_src in (
                ("labelsAtTime", (d.get("evalAtTime") or {}).get("label")),
                ("labelsPostReveal", (d.get("evalPostReveal") or {}).get("label")),
            ):
                if lab_src:
                    act[lab_key][lab_src] = int(act[lab_key].get(lab_src) or 0) + 1
            bucket["observations"] += 1
            bucket["games"].add(gid)
            bucket["refs"].append({"gameId": gid, "decisionId": did})

        game_blob = {
            "gameId": gid,
            "meta": {
                **gmeta,
                "reconstruction": bits,
            },
            "events": rows,
        }
        games_out.append(game_blob)

    if not games_out:
        raise RuntimeError("Нет подходящих новых игр для сборки.")

    # sets → lists for JSON
    situations_out = []
    for bucket in situations.values():
        row = dict(bucket)
        row["games"] = len(row.get("games") or [])
        row["uniqueGames"] = row["games"]
        situations_out.append(row)

    filename, day, seq, pack_id = _next_pack_name()
    created = time.time()
    pack = {
        "format": PACK_FORMAT,
        "handler": PACK_HANDLER,
        "packId": pack_id,
        "filename": filename,
        "createdAt": created,
        "timezone": TZ_NAME,
        "day": day,
        "daySeq": seq,
        "metadata": {
            "packId": pack_id,
            "createdAt": created,
            "timezone": TZ_NAME,
            "format": PACK_FORMAT,
            "handler": PACK_HANDLER,
            "analyzer": "rules-2.1",
            "reconstruct": "2.1",
            "period": {
                "from": min(times) if times else None,
                "to": max(times) if times else None,
            },
            "games": len(games_out),
            "bots": len(bot_ids),
            "decisions": decision_count,
            "situations": len(situations_out),
            "uniqueMatches": len(match_keys),
            "grouping": {
                "mode": "exact",
                "keyVersion": "exact-2",
                "ignores": [],
                "note": "не объединять только по одинаковым трём картам на столе",
            },
        },
        "games": games_out,
        "reconstructed_hands": reconstructed_all,
        "decisions": decisions_all,
        "situations": situations_out,
        "quality_report": quality,
    }
    # checksum = SHA-256 итоговых байтов файла (без вложения хеша в содержимое)
    pack["checksum"] = {"algo": "sha256", "value": None, "scope": "file_bytes"}
    tmp = PACKS_DIR / f".{filename}.tmp"
    final = PACKS_DIR / filename
    tmp.write_text(json.dumps(pack, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    if len(check.get("games") or []) != len(games_out):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Проверка сборника не прошла: число игр.")
    for g in check["games"]:
        if not g.get("events"):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Проверка сборника не прошла: пустая история {g.get('gameId')}")
    tmp.replace(final)
    file_digest = hashlib.sha256(final.read_bytes()).hexdigest()
    # обновить placeholder в файле не будем (циклический хеш) — храним в реестре
    size = final.stat().st_size
    return {
        "packId": pack_id,
        "filename": filename,
        "day": day,
        "daySeq": seq,
        "createdAt": created,
        "timezone": TZ_NAME,
        "period": pack["metadata"]["period"],
        "games": len(games_out),
        "bots": len(bot_ids),
        "decisions": decision_count,
        "situations": len(situations_out),
        "bytes": size,
        "checksum": file_digest,
        "checksumAlgo": "sha256-file-bytes",
        "status": "ready",
        "cleanupStatus": "pending",
        "sourcesDeleted": False,
        "gameIds": [g["gameId"] for g in games_out],
        "path": str(final),
        "analyzer": "rules-2.1",
    }


def _verify_pack_safe_to_cleanup(pack_row: dict) -> tuple[bool, list[str]]:
    """Общая проверка перед любым удалением исходников (ТЗ §9)."""
    reasons: list[str] = []
    filename = pack_row.get("filename")
    if not filename:
        return False, ["нет filename в реестре"]
    path = PACKS_DIR / str(filename)
    if not path.is_file():
        return False, [f"файл сборки отсутствует: {filename}"]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, [f"не читается: {exc}"]
    digest = hashlib.sha256(raw).hexdigest()
    expected = pack_row.get("checksum")
    if isinstance(expected, dict):
        expected = expected.get("value")
    if expected and digest != expected:
        reasons.append("контрольная сумма не совпадает")
    try:
        pack = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, [f"повреждённый JSON: {exc}"]
    if not isinstance(pack, dict):
        return False, ["сборка не dict"]
    if pack.get("format") not in (PACK_FORMAT, "2", 2, "1", 1):
        reasons.append(f"неизвестная схема format={pack.get('format')!r}")
    games = pack.get("games")
    if not isinstance(games, list) or not games:
        reasons.append("в сборке нет games[]")
        return False, reasons
    by_id = {
        str(g.get("gameId")): g
        for g in games
        if isinstance(g, dict) and g.get("gameId")
    }
    for gid in pack_row.get("gameIds") or []:
        g = by_id.get(str(gid))
        if g is None:
            reasons.append(f"игра {gid} отсутствует в файле сборки")
            continue
        events = g.get("events")
        if not isinstance(events, list) or not events:
            reasons.append(f"игра {gid}: нет полной истории events")
            continue
        # исходник не должен отличаться от упакованного
        meta = None
        with _lock:
            index = _read_json(INDEX_FILE, {"games": {}})
            games_idx = index.get("games") if isinstance(index, dict) else {}
            if isinstance(games_idx, dict):
                meta = games_idx.get(str(gid))
        if isinstance(meta, dict):
            src = game_path(str(meta.get("botId") or ""), str(gid))
            if src and src.is_file():
                try:
                    # сравниваем содержимое/хеш, не только число строк
                    src_raw = src.read_bytes()
                    src_digest = hashlib.sha256(src_raw).hexdigest()
                    # битые строки JSONL блокируют удаление (нельзя молча пропускать)
                    src_rows, parse_errs = read_jsonl_strict(src)
                    if parse_errs:
                        reasons.append(
                            f"игра {gid}: повреждённый JSONL "
                            f"({len(parse_errs)} строк), удаление запрещено"
                        )
                    elif src_rows != events:
                        # изменение одной карты при том же числе строк — блок
                        reasons.append(
                            f"игра {gid}: содержимое исходника отличается от сборки "
                            f"(srcSha={src_digest[:12]}…)"
                        )
                except Exception as exc:
                    reasons.append(f"игра {gid}: не удалось сверить исходник: {exc}")
    # реестр обязателен
    reg = _registry()
    registered = any(
        isinstance(r, dict) and r.get("packId") == pack_row.get("packId")
        for r in (reg.get("packs") or [])
    )
    if pack_row.get("packId") and not registered:
        reasons.append("сборка не зарегистрирована в registry.json")
    return (len(reasons) == 0), reasons


def _cleanup_sources(pack_row: dict) -> dict:
    removed = []
    failed = []
    ok, reasons = _verify_pack_safe_to_cleanup(pack_row)
    # packedIn / sourcesDeleted раздельно: помечаем упакованным всегда после регистрации
    def _mark_packed_only() -> None:
        with _lock:
            index = _read_json(INDEX_FILE, {"games": {}})
            games = index.get("games") if isinstance(index, dict) else {}
            if not isinstance(games, dict):
                games = {}
            for gid in pack_row.get("gameIds") or []:
                meta = games.get(gid)
                if not isinstance(meta, dict):
                    continue
                meta["packedIn"] = pack_row.get("packId")
                meta["status"] = "packed"
                meta["sourcesDeleted"] = bool(meta.get("sourcesDeleted"))
                games[gid] = meta
                bot_id = str(meta.get("botId") or "")
                local_path = BOTS_DIR / bot_id / "games.json"
                local = _read_json(local_path, {"games": {}})
                if isinstance(local.get("games"), dict) and gid in local["games"]:
                    local["games"][gid]["packedIn"] = pack_row.get("packId")
                    local["games"][gid]["status"] = "packed"
                    local["games"][gid]["sourcesDeleted"] = bool(
                        local["games"][gid].get("sourcesDeleted")
                    )
                    _atomic_json(local_path, local)
            _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})

    if not AUTO_CLEANUP:
        _mark_packed_only()
        pack_row["cleanupStatus"] = "skipped_disabled"
        pack_row["cleanupRemoved"] = []
        pack_row["cleanupFailed"] = []
        pack_row["cleanupBlocked"] = reasons if not ok else []
        pack_row["sourcesDeleted"] = False
        pack_row["cleanupNote"] = (
            "авто-очистка отключена (BELOT_HISTORY_AUTO_CLEANUP!=1); "
            "игры помечены packedIn, исходники сохранены"
        )
        pack_row["status"] = "ready"
        with _lock:
            reg = _registry()
            packs = reg.get("packs") or []
            for i, row in enumerate(packs):
                if row.get("packId") == pack_row.get("packId"):
                    packs[i] = pack_row
                    break
            reg["packs"] = packs
            _save_registry(reg)
        return pack_row
    if not ok:
        pack_row["cleanupStatus"] = "blocked"
        pack_row["cleanupRemoved"] = []
        pack_row["cleanupFailed"] = []
        pack_row["cleanupBlocked"] = reasons
        pack_row["status"] = "ready_cleanup_pending"
        with _lock:
            reg = _registry()
            packs = reg.get("packs") or []
            for i, row in enumerate(packs):
                if row.get("packId") == pack_row.get("packId"):
                    packs[i] = pack_row
                    break
            reg["packs"] = packs
            _save_registry(reg)
        return pack_row

    with _lock:
        index = _read_json(INDEX_FILE, {"games": {}})
        games = index.get("games") if isinstance(index, dict) else {}
        if not isinstance(games, dict):
            games = {}
        for gid in pack_row.get("gameIds") or []:
            meta = games.get(gid)
            if not isinstance(meta, dict):
                continue
            bot_id = str(meta.get("botId") or "")
            path = game_path(bot_id, gid)
            try:
                if path and path.is_file():
                    path.unlink()
                meta["packedIn"] = pack_row.get("packId")
                meta["status"] = "packed"
                meta["file"] = None
                meta["bytes"] = 0
                games[gid] = meta
                # local bot index
                local_path = BOTS_DIR / bot_id / "games.json"
                local = _read_json(local_path, {"games": {}})
                if isinstance(local.get("games"), dict) and gid in local["games"]:
                    local["games"][gid]["packedIn"] = pack_row.get("packId")
                    local["games"][gid]["status"] = "packed"
                    local["games"][gid]["file"] = None
                    _atomic_json(local_path, local)
                removed.append(gid)
            except OSError as exc:
                failed.append({"gameId": gid, "error": str(exc)})
        _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
    status = "done" if not failed else "partial"
    pack_row["cleanupStatus"] = status
    pack_row["cleanupRemoved"] = removed
    pack_row["cleanupFailed"] = failed
    pack_row["cleanupBlocked"] = []
    if failed:
        pack_row["status"] = "ready_cleanup_pending"
    else:
        pack_row["status"] = "ready"
    # update registry entry
    with _lock:
        reg = _registry()
        packs = reg.get("packs") or []
        for i, row in enumerate(packs):
            if row.get("packId") == pack_row.get("packId"):
                packs[i] = pack_row
                break
        reg["packs"] = packs
        _save_registry(reg)
    return pack_row


def start_pack_job(game_ids: list[str], *, idempotency_key: str = "") -> dict:
    global _jobs_running
    ensure_dirs()
    ids = []
    seen = set()
    for gid in game_ids:
        gid = str(gid or "").strip()
        if not gid or gid in seen:
            continue
        if not SAFE_ID.match(gid):
            raise RuntimeError(f"Некорректный id игры: {gid}")
        seen.add(gid)
        ids.append(gid)
    if not ids:
        raise RuntimeError("Нет подходящих новых игр для сборки.")

    with _lock:
        if _jobs_running >= MAX_JOBS:
            raise RuntimeError("Уже выполняется максимум заданий сборки. Подождите.")
        if idempotency_key:
            for path in JOBS_DIR.glob("*.json"):
                job = _read_json(path, {})
                if job.get("idempotencyKey") == idempotency_key and job.get("status") in (
                    "queued", "running", "ready", "cleanup", "done", "done_cleanup_pending", "error"
                ):
                    return job
        index = _read_json(INDEX_FILE, {"games": {}})
        games = index.get("games") or {}
        selected = []
        for gid in ids:
            meta = games.get(gid)
            if not isinstance(meta, dict):
                continue
            if meta.get("packedIn"):
                continue
            if meta.get("status") == "processing":
                continue
            if str(meta.get("status")) != "completed":
                continue
            selected.append(gid)
        if not selected:
            raise RuntimeError("Нет подходящих новых игр для сборки.")
        for gid in selected:
            games[gid]["status"] = "processing"
            games[gid]["lockedAt"] = time.time()
        _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = {
            "jobId": job_id,
            "idempotencyKey": idempotency_key or job_id,
            "status": "queued",
            "createdAt": time.time(),
            "gameIds": selected,
            "progress": {"checked": 0, "processed": 0, "left": len(selected), "situations": 0, "errors": []},
            "errors": [],
            "pack": None,
        }
        _atomic_json(JOBS_DIR / f"{job_id}.json", job)
        _jobs_running += 1

    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return job


def _run_job(job_id: str) -> None:
    global _jobs_running
    path = JOBS_DIR / f"{job_id}.json"
    job = _read_json(path, {})
    try:
        job["status"] = "running"
        job["startedAt"] = time.time()
        _atomic_json(path, job)
        pack_row = _build_pack(job)
        job["pack"] = pack_row
        job["status"] = "cleanup"
        _atomic_json(path, job)
        with _lock:
            reg = _registry()
            reg.setdefault("packs", []).append(pack_row)
            _save_registry(reg)
        pack_row = _cleanup_sources(pack_row)
        try:
            reconcile_stale_games()
        except Exception:
            pass
        job["pack"] = pack_row
        job["status"] = "done" if pack_row.get("cleanupStatus") == "done" else "done_cleanup_pending"
        job["finishedAt"] = time.time()
        job["progress"] = {
            "checked": len(job.get("gameIds") or []),
            "processed": pack_row.get("games") or 0,
            "left": 0,
            "situations": pack_row.get("situations") or 0,
            "errors": job.get("errors") or [],
        }
        _atomic_json(path, job)
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        job["finishedAt"] = time.time()
        # unlock games
        with _lock:
            index = _read_json(INDEX_FILE, {"games": {}})
            games = index.get("games") or {}
            for gid in job.get("gameIds") or []:
                meta = games.get(gid)
                if isinstance(meta, dict) and meta.get("status") == "processing" and not meta.get("packedIn"):
                    meta["status"] = "completed"
                    games[gid] = meta
            _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
        _atomic_json(path, job)
    finally:
        with _lock:
            _jobs_running = max(0, _jobs_running - 1)


def retry_cleanup(pack_id: str) -> dict:
    reg = _registry()
    packs = reg.get("packs") or []
    found = None
    for row in packs:
        if row.get("packId") == pack_id:
            found = row
            break
    if not found:
        raise RuntimeError("Сборник не найден.")
    if found.get("cleanupStatus") == "done":
        return found
    # ensure pack file exists before cleanup
    path = PACKS_DIR / str(found.get("filename"))
    if not path.is_file():
        raise RuntimeError("Файл сборника отсутствует — очистку нельзя продолжить.")
    return _cleanup_sources(found)


def job_status(job_id: str) -> dict:
    if not SAFE_ID.match(job_id or ""):
        raise RuntimeError("Некорректный id задания.")
    job = _read_json(JOBS_DIR / f"{job_id}.json", None)
    if not isinstance(job, dict):
        raise RuntimeError("Задание не найдено.")
    return job


def recover_jobs() -> None:
    """После рестарта: продолжить cleanup или пометить незавершённые."""
    ensure_dirs()
    global _jobs_running
    for path in JOBS_DIR.glob("*.json"):
        job = _read_json(path, {})
        if not isinstance(job, dict):
            continue
        status = job.get("status")
        if status == "cleanup" and isinstance(job.get("pack"), dict):
            try:
                pack_row = _cleanup_sources(job["pack"])
                job["pack"] = pack_row
                job["status"] = "done" if pack_row.get("cleanupStatus") == "done" else "done_cleanup_pending"
                _atomic_json(path, job)
            except Exception as exc:
                job["status"] = "error"
                job["error"] = f"cleanup recovery: {exc}"
                _atomic_json(path, job)
        elif status in ("queued", "running"):
            # не удаляем исходники — разблокируем игры
            with _lock:
                index = _read_json(INDEX_FILE, {"games": {}})
                games = index.get("games") or {}
                for gid in job.get("gameIds") or []:
                    meta = games.get(gid)
                    if isinstance(meta, dict) and meta.get("status") == "processing" and not meta.get("packedIn"):
                        meta["status"] = "completed"
                        games[gid] = meta
                _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
            job["status"] = "error"
            job["error"] = "прервано перезапуском панели"
            _atomic_json(path, job)
    _jobs_running = 0


def _empty_agent_stats(agent: str) -> dict:
    return {
        "agent": agent,
        "games": 0,
        "wins": 0,
        "losses": 0,
        "otherResults": 0,
        "ourMoves": 0,
        "missedTake": 0,
        "overpay": 0,
        "good": 0,
        "bad": 0,
        "disputed": 0,
        "uneval": 0,
        "betterTotal": 0,
        "logins": [],
        "sources": {"openGames": 0, "packs": 0},
    }


def _classify_our_decision(decision: dict) -> str | None:
    """missed_take | overpay | good | disputed | uneval | None."""
    if not decision.get("us"):
        return None
    ep = decision.get("evalPostReveal") or decision.get("evalAtTime") or {}
    expl = str(ep.get("explanation") or "")
    label = str(ep.get("label") or "")
    # Analyzer wording drifted: "забирающая текущий стол" / "более дешёвая
    # допустимая бьющая" — keep old needles so older packs still classify.
    if "взятка отдана" in expl and (
        "бьющая текущий стол" in expl or "забирающая текущий стол" in expl
    ):
        return "missed_take"
    if "более дешёвая" in expl and "бьющ" in expl:
        return "overpay"
    if label == "удачный":
        return "good"
    if label == "неудачный":
        return "bad"
    if label == "спорный":
        return "disputed"
    if label in ("неоценённый", "", "?"):
        return "uneval"
    return None


def _accumulate_agent_game(stats: dict, *, result, decisions: list, source: str) -> None:
    stats["games"] += 1
    stats["sources"][source] = int(stats["sources"].get(source) or 0) + 1
    res = str(result or "")
    if "побед" in res.casefold() or res.casefold() in ("win", "won", "победа"):
        stats["wins"] += 1
    elif "пораж" in res.casefold() or res.casefold() in ("loss", "lost", "поражение"):
        stats["losses"] += 1
    else:
        stats["otherResults"] += 1
    for d in decisions or []:
        kind = _classify_our_decision(d)
        if kind is None:
            continue
        stats["ourMoves"] += 1
        if kind == "missed_take":
            stats["missedTake"] += 1
            stats["betterTotal"] += 1
            stats["bad"] += 1
        elif kind == "overpay":
            stats["overpay"] += 1
            stats["betterTotal"] += 1
            stats["bad"] += 1
        elif kind == "good":
            stats["good"] += 1
        elif kind == "bad":
            stats["bad"] += 1
            stats["betterTotal"] += 1
        elif kind == "disputed":
            stats["disputed"] += 1
        elif kind == "uneval":
            stats["uneval"] += 1


def _finalize_agent_stats(stats: dict) -> dict:
    decided = stats["wins"] + stats["losses"]
    moves = stats["ourMoves"] or 0
    stats["winRate"] = round(100.0 * stats["wins"] / decided, 1) if decided else None
    stats["missedTakePct"] = round(100.0 * stats["missedTake"] / moves, 1) if moves else None
    stats["overpayPct"] = round(100.0 * stats["overpay"] / moves, 1) if moves else None
    stats["betterPct"] = round(100.0 * stats["betterTotal"] / moves, 1) if moves else None
    stats["goodPct"] = round(100.0 * stats["good"] / moves, 1) if moves else None
    stats["errorRate"] = stats["betterPct"]  # «ошибочные» по rules-1
    return stats


def compare_experience_agents(*, max_games: int = 80) -> dict:
    """Сравнение fallen / fallen2 по открытым играм и сборникам опыта."""
    ensure_dirs()
    index = _read_json(INDEX_FILE, None)
    if not isinstance(index, dict) or not isinstance(index.get("games"), dict):
        index = rebuild_index()
    rows = [
        r for r in (index.get("games") or {}).values()
        if isinstance(r, dict) and str(r.get("status")) == "completed"
    ]
    rows.sort(key=lambda r: float(r.get("startedAt") or 0), reverse=True)
    rows = rows[: max(1, min(int(max_games or 80), 200))]

    by_agent: dict[str, dict] = {}
    analyzed = 0
    errors = []
    seen_games: set[str] = set()

    for meta in rows:
        agent = str(meta.get("agent") or "unknown")
        bot_id = str(meta.get("botId") or "")
        gid = str(meta.get("gameId") or "")
        if not gid or gid in seen_games:
            continue
        path = game_path(bot_id, gid)
        if path is None:
            continue
        try:
            events = read_jsonl_tolerant(path)
            _recon, decisions, _bits = _run_game_analysis(events)
        except Exception as exc:
            errors.append({"gameId": gid, "error": str(exc)})
            continue
        seen_games.add(gid)
        stats = by_agent.setdefault(agent, _empty_agent_stats(agent))
        login = str(meta.get("login") or "")
        if login and login not in stats["logins"]:
            stats["logins"].append(login)
        _accumulate_agent_game(
            stats,
            result=meta.get("result"),
            decisions=decisions,
            source="openGames",
        )
        analyzed += 1

    # Сборники опыта (если есть) — только игры, которых уже нет в открытых
    for prow in _registry().get("packs") or []:
        if not isinstance(prow, dict):
            continue
        fname = prow.get("filename")
        if not fname:
            continue
        path = PACKS_DIR / str(fname)
        if not path.is_file():
            continue
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append({"pack": fname, "error": str(exc)})
            continue
        decisions = pack.get("decisions") or []
        games = pack.get("games") or []
        by_game: dict[str, list] = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            by_game.setdefault(str(d.get("gameId") or ""), []).append(d)
        for g in games:
            if not isinstance(g, dict):
                continue
            gmeta = g.get("meta") or {}
            agent = str(gmeta.get("agent") or "unknown")
            gid = str(g.get("gameId") or gmeta.get("gameId") or "")
            if not gid or gid in seen_games:
                continue
            seen_games.add(gid)
            stats = by_agent.setdefault(agent, _empty_agent_stats(agent))
            login = str(gmeta.get("login") or "")
            if login and login not in stats["logins"]:
                stats["logins"].append(login)
            _accumulate_agent_game(
                stats,
                result=gmeta.get("result"),
                decisions=by_game.get(gid) or [],
                source="packs",
            )
            analyzed += 1

    agents = [_finalize_agent_stats(s) for s in by_agent.values()]
    # Лучше = меньше % «можно было лучше» при достаточной выборке
    ranked = sorted(
        [a for a in agents if (a.get("ourMoves") or 0) >= 20],
        key=lambda a: (
            a["errorRate"] if a.get("errorRate") is not None else 999,
            -(a.get("winRate") or 0),
            -a.get("games", 0),
        ),
    )
    best = ranked[0]["agent"] if ranked else None
    pair = {a["agent"]: a for a in agents if a["agent"] in ("fallen", "fallen2")}
    verdict = None
    if "fallen" in pair and "fallen2" in pair:
        a1, a2 = pair["fallen"], pair["fallen2"]
        if a1.get("ourMoves", 0) >= 20 and a2.get("ourMoves", 0) >= 20:
            d_err = (a1.get("errorRate") or 0) - (a2.get("errorRate") or 0)
            d_miss = (a1.get("missedTakePct") or 0) - (a2.get("missedTakePct") or 0)
            if d_err > 2:
                verdict = (
                    f"fallen2 пока лучше: на {d_err:.1f} п.п. меньше ходов "
                    f"«можно было лучше», не взяли −{d_miss:.1f} п.п."
                )
            elif d_err < -2:
                verdict = (
                    f"fallen пока лучше: у fallen2 на {-d_err:.1f} п.п. больше "
                    f"ходов «можно было лучше»"
                )
            else:
                verdict = "Пока почти равны по % ошибок rules-1 — нужно больше игр."
        else:
            verdict = "Мало ходов для вердикта — доиграйте и соберите опыт."
    elif not agents:
        verdict = "Нет завершённых игр с историей. Запустите fallen и fallen2."
    else:
        verdict = "Нужны игры обоих агентов (fallen и fallen2)."

    return {
        "agents": sorted(agents, key=lambda a: a.get("agent") or ""),
        "best": best,
        "verdict": verdict,
        "analyzedGames": analyzed,
        "errors": errors[:20],
        "note": (
            "Сравнение по разбору rules-1: не взяли / переплата / удачные. "
            "Руки часто partial — это черновой % для A/B fallen vs fallen2, "
            "не доказательство силы."
        ),
    }


def apply_history_line(payload: dict) -> None:
    """Обновление индекса из stdout бота [HISTORY]..."""
    if not isinstance(payload, dict):
        return
    meta = {
        "gameId": payload.get("gameId"),
        "botId": payload.get("botId"),
        "login": payload.get("login"),
        "status": payload.get("status"),
        "agent": payload.get("agent"),
        "agentVersion": payload.get("agentVersion"),
        "variant": payload.get("variant"),
        "points": payload.get("points"),
        "startedAt": payload.get("startedAt"),
        "endedAt": payload.get("endedAt"),
        "hands": payload.get("hands"),
        "decisions": payload.get("decisions"),
        "result": payload.get("result"),
        "scores": payload.get("scores"),
        "matchKey": payload.get("matchKey"),
        "seat": payload.get("seat"),
        "file": Path(str(payload.get("path"))).name if payload.get("path") else None,
        "bytes": Path(str(payload["path"])).stat().st_size if payload.get("path") and Path(str(payload["path"])).is_file() else None,
        "reconstructionStatus": payload.get("reconstructionStatus"),
        "analysisStatus": payload.get("analysisStatus"),
    }
    upsert_game_meta(meta)
    # новая партия — старые running этого бота больше не «текущие»
    if str(meta.get("status") or "") == "running" and meta.get("botId") and meta.get("gameId"):
        try:
            with _lock:
                data = _read_json(INDEX_FILE, {"games": {}})
                games = data.get("games") if isinstance(data, dict) else {}
                if not isinstance(games, dict):
                    return
                bot_id = str(meta["botId"])
                cur = str(meta["gameId"])
                now = time.time()
                changed = False
                for gid, row in list(games.items()):
                    if not isinstance(row, dict) or str(gid) == cur:
                        continue
                    if str(row.get("botId") or "") != bot_id:
                        continue
                    if row.get("packedIn"):
                        continue
                    if str(row.get("status") or "") != "running":
                        continue
                    row = dict(row)
                    row["status"] = "interrupted"
                    row["endedAt"] = row.get("endedAt") or now
                    row["quality"] = "incomplete"
                    games[gid] = row
                    changed = True
                if changed:
                    _atomic_json(INDEX_FILE, {"games": games, "updatedAt": time.time()})
        except Exception:
            pass

def save_owner_review_mark(data: dict) -> dict:
    """Сохранить разметку владельца отдельно от авто-оценок (ТЗ §13)."""
    ensure_dirs()
    marks_path = HISTORY_ROOT / "owner_review_marks.json"
    with _lock:
        store = _read_json(marks_path, {"marks": [], "updatedAt": None})
        if not isinstance(store, dict):
            store = {"marks": []}
        marks = store.get("marks")
        if not isinstance(marks, list):
            marks = []
        mark = {
            "id": f"m_{uuid.uuid4().hex[:12]}",
            "t": time.time(),
            "gameId": data.get("gameId"),
            "decisionId": data.get("decisionId"),
            "packId": data.get("packId"),
            "ownerMark": data.get("ownerMark") or data.get("mark"),
            "ownerPreferredCard": data.get("ownerPreferredCard") or data.get("preferredCard"),
            "ownerNote": data.get("ownerNote") or data.get("note"),
            "sourceAnalyzer": data.get("analyzer"),
        }
        if mark["ownerMark"] not in ("правильно", "ошибка", "спорно", None, ""):
            raise RuntimeError("ownerMark: правильно | ошибка | спорно")
        # заменить предыдущую разметку того же decision
        marks = [
            m for m in marks
            if not (
                isinstance(m, dict)
                and m.get("decisionId")
                and m.get("decisionId") == mark["decisionId"]
                and m.get("gameId") == mark["gameId"]
            )
        ]
        marks.append(mark)
        store["marks"] = marks
        store["updatedAt"] = time.time()
        _atomic_json(marks_path, store)
    return mark
