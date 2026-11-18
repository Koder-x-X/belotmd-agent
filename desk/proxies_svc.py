"""Список прокси для desk: хранение + проверка + выбор рабочего при старте бота."""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PROXIES_FILE = ROOT / "proxies.json"
PROBE_URL = "https://belot.md/"
PROBE_TIMEOUT_S = 4.0
CACHE_TTL_S = 60.0
# Don't probe the whole free-proxy dump on every seat — find one and go.
PICK_BATCH = 12
PICK_MAX_TRIES = 48  # stop after this many probes if none work

_lock = threading.Lock()
_cache: dict = {"t": 0.0, "results": []}  # results: [{url, ok, ms, error}]
_rr = 0
_last_good: str = ""
_blacklist: dict[str, float] = {}  # url -> monotonic until
BLACKLIST_S = 120.0


def blacklist(url: str, seconds: float = BLACKLIST_S) -> None:
    u = normalize(url or "")
    if not u:
        return
    with _lock:
        _blacklist[u] = time.monotonic() + max(5.0, float(seconds))


def _is_blacklisted(url: str) -> bool:
    u = normalize(url or "")
    with _lock:
        until = float(_blacklist.get(u) or 0.0)
        if until and time.monotonic() < until:
            return True
        if u in _blacklist:
            _blacklist.pop(u, None)
    return False


def _default() -> dict:
    return {
        "enabled": False,
        "proxies": [],
        "updatedAt": 0,
    }


def load() -> dict:
    with _lock:
        if not PROXIES_FILE.is_file():
            return _default()
        try:
            data = json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _default()
        if not isinstance(data, dict):
            return _default()
        rows = data.get("proxies") or []
        if not isinstance(rows, list):
            rows = []
        cleaned = []
        for item in rows:
            url = normalize(str(item or ""))
            if url and url not in cleaned:
                cleaned.append(url)
        return {
            "enabled": bool(data.get("enabled")),
            "proxies": cleaned,
            "updatedAt": float(data.get("updatedAt") or 0),
        }


def save(*, enabled: bool, proxies: list[str]) -> dict:
    cleaned = []
    for item in proxies:
        url = normalize(str(item or ""))
        if url and url not in cleaned:
            cleaned.append(url)
    payload = {
        "enabled": bool(enabled),
        "proxies": cleaned,
        "updatedAt": time.time(),
    }
    with _lock:
        PROXIES_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _cache["t"] = 0.0
        _cache["results"] = []
    return payload


def normalize(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text.startswith("#"):
        return ""
    if "://" not in text:
        text = "http://" + text
    text = text.rstrip("/")
    parsed = urlparse(text)
    if not parsed.hostname:
        return ""
    if parsed.scheme.lower() not in ("http", "https", "socks4", "socks5", "socks5h"):
        return ""
    return text


def redact(url: str) -> str:
    """Hide user:pass in logs / UI."""
    try:
        p = urlparse(url)
        if p.username or p.password:
            host = p.hostname or ""
            port = f":{p.port}" if p.port else ""
            return f"{p.scheme}://***@{host}{port}"
        return url
    except Exception:
        return url


def host_label(url: str) -> str:
    try:
        p = urlparse(url)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        return f"{host}{port}"
    except Exception:
        return "?"


def probe_one(url: str, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """Can belot.md be reached through this proxy? HTTP(S) only for desk login."""
    url = normalize(url)
    if not url:
        return {"url": url, "ok": False, "ms": None, "error": "пустой"}
    if urlparse(url).scheme.lower().startswith("socks"):
        # Login uses urllib ProxyHandler (HTTP CONNECT). SOCKS is fine for the
        # Node bridge later, but we require an HTTP probe proxy for seat start.
        return {
            "url": url,
            "ok": False,
            "ms": None,
            "error": "socks — только для bridge; для входа нужен http://",
        }
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    req = urllib.request.Request(
        PROBE_URL,
        headers={"User-Agent": "belotmd-agent-desk-proxy-probe"},
        method="GET",
    )
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            code = getattr(resp, "status", None) or resp.getcode()
        ms = int((time.monotonic() - t0) * 1000)
        ok = int(code or 0) < 500
        return {
            "url": url,
            "ok": ok,
            "ms": ms,
            "error": None if ok else f"HTTP {code}",
        }
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        return {"url": url, "ok": False, "ms": ms, "error": str(exc.reason if hasattr(exc, "reason") else exc)}


def probe_all(
    proxies: list[str] | None = None,
    *,
    force: bool = False,
    limit: int = 40,
) -> list[dict]:
    """UI «Проверить сейчас»: sample up to `limit` entries (full dump is huge)."""
    cfg = load()
    rows = proxies if proxies is not None else list(cfg.get("proxies") or [])
    rows = [normalize(str(u)) for u in rows]
    rows = [u for u in rows if u]
    sample = rows[: max(1, int(limit or 40))]
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cache["results"]
            and (now - float(_cache["t"] or 0)) < CACHE_TTL_S
        ):
            return list(_cache["results"])
    results: list[dict] = []
    if not sample:
        return results
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(sample)))) as pool:
        futs = {pool.submit(probe_one, u): u for u in sample}
        for fut in as_completed(futs):
            results.append(fut.result())
    order = {u: i for i, u in enumerate(sample)}
    results.sort(key=lambda r: order.get(r.get("url"), 999))
    with _lock:
        _cache["t"] = time.monotonic()
        _cache["results"] = list(results)
    return results


def pick_working(*, prefer: str = "", exclude: set[str] | None = None) -> tuple[str, dict]:
    """Return (proxy_url, meta). Empty url = go direct (proxies disabled).

    Probes in small parallel batches and returns on the FIRST success so a
    700-line free-proxy dump cannot freeze «Сесть за стол» for minutes.
    """
    global _rr, _last_good
    cfg = load()
    if not cfg.get("enabled"):
        return "", {"mode": "direct", "reason": "прокси выключены"}
    rows = list(cfg.get("proxies") or [])
    if not rows:
        raise RuntimeError(
            "Прокси включены, но список пуст. Откройте «Прокси» и добавьте адреса."
        )
    skip = {normalize(x) for x in (exclude or set()) if normalize(x)}

    # Prefer last-known-good / explicit prefer, then rotate through the list.
    ordered: list[str] = []
    for candidate in (prefer, _last_good):
        c = normalize(candidate or "")
        if c and c in rows and c not in ordered and c not in skip and not _is_blacklisted(c):
            ordered.append(c)
    with _lock:
        start = _rr % len(rows)
        _rr += 1
    for i in range(len(rows)):
        u = rows[(start + i) % len(rows)]
        if u in ordered or u in skip or _is_blacklisted(u):
            continue
        ordered.append(u)

    tried = 0
    fails: list[dict] = []
    while tried < min(len(ordered), PICK_MAX_TRIES):
        batch = ordered[tried:tried + PICK_BATCH]
        tried += len(batch)
        with ThreadPoolExecutor(max_workers=min(PICK_BATCH, len(batch))) as pool:
            futs = [pool.submit(probe_one, u) for u in batch]
            for fut in as_completed(futs):
                row = fut.result()
                if row.get("ok") and row.get("url"):
                    # Cancel is cooperative; remaining probes finish but we
                    # already have a seat path — return immediately.
                    _last_good = row["url"]
                    with _lock:
                        # Soft-update cache entry for UI probe page.
                        cached = list(_cache.get("results") or [])
                        cached = [r for r in cached if r.get("url") != row["url"]]
                        cached.insert(0, row)
                        _cache["results"] = cached[:200]
                        _cache["t"] = time.monotonic()
                    return row["url"], {
                        "mode": "proxy",
                        "ms": row.get("ms"),
                        "host": host_label(row["url"]),
                        "redacted": redact(row["url"]),
                        "okCount": 1,
                        "total": len(rows),
                        "tried": tried,
                    }
                fails.append(row)

    sample = "; ".join(
        f"{host_label(r.get('url') or '?')}: {r.get('error') or 'fail'}"
        for r in fails[:4]
    )
    raise RuntimeError(
        f"Не нашёл рабочий прокси (проверил {tried} из {len(rows)}). "
        + (f"Примеры отказов: {sample}" if sample else "Проверьте список.")
    )
