"""Проверка качества без self-play: совпадение с разметкой на hold-out."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .model import ActionModel
from . import paths as _paths
from ..agents.heuristic_agent import HeuristicAgent
from ..agents.random_agent import RandomAgent
from ..agents.strong_agent import StrongAgent, choose_heuristic
from ..game.state import BelotState


def _accuracy(pred, y, acceptable=None):
    if len(y) == 0:
        return None
    exact = float((pred == y).mean())
    return {"exact": exact, "n": int(len(y))}


def evaluate_model(model_path, dataset_dir, part="test") -> dict:
    _paths.ensure_dirs()
    dataset_dir = Path(dataset_dir)
    data_path = dataset_dir / f"{part}.npz"
    if not data_path.exists():
        return {"ok": False, "error": f"нет {data_path}"}
    data = np.load(data_path)
    X, y, M = data["X"], data["y"], data["M"]
    if len(X) == 0:
        return {
            "ok": False,
            "error": "пустой проверочный набор",
            "note": "Совпадение с разметкой не доказывает рост побед.",
        }

    model = ActionModel.load(model_path)
    t0 = time.monotonic()
    scores = np.stack([model.predict_scores(x) for x in X])
    # mask illegal
    masked = np.where(M.astype(bool), scores, -1e9)
    pred = masked.argmax(axis=1)
    dt = time.monotonic() - t0

    # Некорректные до резерва: argmax без маски
    raw_pred = scores.argmax(axis=1)
    illegal_rate = float((M[np.arange(len(y)), raw_pred] == 0).mean()) if len(y) else 0.0

    # Fallback rate: если модель выбрала illegal — эвристика бы спасла
    fallback = float((M[np.arange(len(y)), pred] == 0).mean()) if len(y) else 0.0

    result = {
        "ok": True,
        "model": model.version,
        "part": part,
        "n": int(len(y)),
        "exact_match": float((pred == y).mean()),
        "illegal_before_mask": illegal_rate,
        "fallback_needed": fallback,
        "mean_ms": (dt / max(len(y), 1)) * 1000,
        "note": "Совпадение с предпочтительным действием ≠ процент побед. "
                "Альтернативный ход из журнала несыгран.",
    }
    return result


def compare_agents_on_masks(dataset_dir, part="test", checkpoint=None) -> dict:
    """Сравнение strong/heuristic/random на одних и тех же масках (по y как proxy)."""
    data_path = Path(dataset_dir) / f"{part}.npz"
    data = np.load(data_path)
    X, y, M = data["X"], data["y"], data["M"]
    out = {"n": int(len(y)), "agents": {}}

    # Model / strong scores if checkpoint
    if checkpoint:
        model = ActionModel.load(checkpoint)
        scores = np.stack([model.predict_scores(x) for x in X])
        pred = np.where(M.astype(bool), scores, -1e9).argmax(axis=1)
        out["agents"]["model"] = float((pred == y).mean())

    # Random baseline: expected match ~ mean(1/n_legal)
    n_legal = M.sum(axis=1).clip(min=1)
    out["agents"]["random_expected"] = float((1.0 / n_legal).mean())

    # Heuristic cannot run on feature vectors alone without state — skip full
    # replay here; report only model/random proxy.
    out["note"] = "Полное сравнение heuristic требует восстановления state; "
                  "здесь proxy по разметке для модели."
    return out


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="belot-learn-eval")
    p.add_argument("model")
    p.add_argument("dataset")
    p.add_argument("--part", default="test")
    args = p.parse_args(argv)
    ds = Path(args.dataset)
    if not ds.is_absolute():
        ds = _paths.DATASETS_DIR / args.dataset
    mp = Path(args.model)
    if not mp.is_absolute():
        mp = _paths.MODELS_DIR / args.model
    result = evaluate_model(mp, ds, part=args.part)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
