"""Реестр моделей: список, активация, откат. Активная не меняется после обучения сама."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import paths as _paths


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_models() -> list[dict]:
    _paths.ensure_dirs()
    reg = _read_json(_paths.REGISTRY_FILE, {"models": []})
    return list(reg.get("models") or [])


def register_model(meta: dict, directory: Path) -> dict:
    _paths.ensure_dirs()
    reg = _read_json(_paths.REGISTRY_FILE, {"models": [], "history": []})
    entry = {
        "id": meta["id"],
        "path": str(Path(directory).resolve()),
        "created": meta.get("created", time.time()),
        "architecture": meta.get("architecture"),
        "feature_version": meta.get("feature_version"),
        "metrics": meta.get("metrics") or meta.get("train") or {},
        "dataset": meta.get("dataset"),
        "notes": meta.get("notes", ""),
    }
    models = [m for m in reg["models"] if m.get("id") != entry["id"]]
    models.append(entry)
    reg["models"] = models
    _write_json(_paths.REGISTRY_FILE, reg)
    return entry


def active_checkpoint() -> Optional[Path]:
    _paths.ensure_dirs()
    data = _read_json(_paths.ACTIVE_FILE, {})
    path = data.get("path")
    if not path:
        return None
    p = Path(path)
    if (p / "weights.npz").exists():
        return p
    if p.exists() and p.suffix == ".npz":
        return p
    return None


def active_info() -> dict:
    return _read_json(_paths.ACTIVE_FILE, {})


def activate_model(model_id: str) -> dict:
    models = list_models()
    match = next((m for m in models if m["id"] == model_id), None)
    if match is None:
        raise ValueError(f"модель {model_id!r} не найдена в реестре")
    path = Path(match["path"])
    if not (path / "weights.npz").exists():
        raise ValueError(f"нет weights.npz у {path}")
    prev = active_info()
    reg = _read_json(_paths.REGISTRY_FILE, {"models": [], "history": []})
    reg.setdefault("history", []).append({
        "t": time.time(),
        "action": "activate",
        "id": model_id,
        "previous": prev.get("id"),
    })
    _write_json(_paths.REGISTRY_FILE, reg)
    info = {
        "id": model_id,
        "path": str(path),
        "activated_at": time.time(),
        "previous_id": prev.get("id"),
    }
    _write_json(_paths.ACTIVE_FILE, info)
    return info


def rollback_model() -> dict:
    """Откат к previous_id, если есть."""
    cur = active_info()
    prev_id = cur.get("previous_id")
    if not prev_id:
        reg = _read_json(_paths.REGISTRY_FILE, {"models": [], "history": []})
        hist = [h for h in reg.get("history") or [] if h.get("action") == "activate"]
        if len(hist) >= 2:
            prev_id = hist[-2].get("id")
        else:
            raise ValueError("некуда откатываться: нет предыдущей активной модели")
    return activate_model(prev_id)


def clear_active():
    _paths.ensure_dirs()
    if _paths.ACTIVE_FILE.exists():
        _paths.ACTIVE_FILE.unlink()
