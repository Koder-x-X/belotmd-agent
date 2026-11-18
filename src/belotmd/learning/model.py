"""Модель выбора действия: обучение через torch (опционально), инференс на numpy."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .features import ACTION_SPACE, FEATURE_VERSION, feature_dim
from . import paths as _paths


class ActionModel:
    """Двухслойный MLP. Веса в .npz + meta.json."""

    def __init__(self, weights: dict, meta: dict):
        self.weights = weights
        self.meta = meta
        self.version = meta.get("id") or meta.get("version") or "unknown"

    @property
    def feature_version(self) -> str:
        return self.meta.get("feature_version", FEATURE_VERSION)

    def predict_scores(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).ravel()
        w1 = self.weights["w1"]
        b1 = self.weights["b1"]
        w2 = self.weights["w2"]
        b2 = self.weights["b2"]
        h = np.tanh(x @ w1 + b1)
        return h @ w2 + b2

    def save(self, directory, meta_update=None) -> Path:
        _paths.ensure_dirs()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta)
        if meta_update:
            meta.update(meta_update)
        np.savez(
            directory / "weights.npz",
            w1=self.weights["w1"],
            b1=self.weights["b1"],
            w2=self.weights["w2"],
            b2=self.weights["b2"],
        )
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.meta = meta
        self.version = meta.get("id", self.version)
        return directory

    @classmethod
    def load(cls, path) -> "ActionModel":
        path = Path(path)
        if path.is_file() and path.suffix == ".npz":
            directory = path.parent
        else:
            directory = path
        weights_file = directory / "weights.npz"
        meta_file = directory / "meta.json"
        if not weights_file.exists():
            raise FileNotFoundError(f"нет weights.npz в {directory}")
        data = np.load(weights_file)
        weights = {
            "w1": data["w1"].astype(np.float32),
            "b1": data["b1"].astype(np.float32),
            "w2": data["w2"].astype(np.float32),
            "b2": data["b2"].astype(np.float32),
        }
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        # Совместимость признаков
        if meta.get("feature_version") and meta["feature_version"] != FEATURE_VERSION:
            raise ValueError(
                f"несовместимая модель: feature_version "
                f"{meta.get('feature_version')} != {FEATURE_VERSION}"
            )
        expected = meta.get("feature_dim") or feature_dim()
        if weights["w1"].shape[0] != expected:
            raise ValueError(
                f"несовместимая размерность признаков: "
                f"{weights['w1'].shape[0]} != {expected}"
            )
        return cls(weights, meta)

    @classmethod
    def random_init(cls, hidden=128, seed=0, **meta) -> "ActionModel":
        rng = np.random.default_rng(seed)
        d = feature_dim()
        w1 = (rng.normal(0, 0.05, size=(d, hidden))).astype(np.float32)
        b1 = np.zeros(hidden, dtype=np.float32)
        w2 = (rng.normal(0, 0.05, size=(hidden, ACTION_SPACE))).astype(np.float32)
        b2 = np.zeros(ACTION_SPACE, dtype=np.float32)
        mid = {
            "id": meta.get("id") or f"model-{time.strftime('%Y%m%d-%H%M%S')}",
            "created": time.time(),
            "architecture": f"mlp_{hidden}",
            "feature_version": FEATURE_VERSION,
            "feature_dim": d,
            "action_space": ACTION_SPACE,
            "rules_version": "belotmd-state-1",
            **meta,
        }
        return cls({"w1": w1, "b1": b1, "w2": w2, "b2": b2}, mid)


def train_numpy(
    X: np.ndarray,
    y: np.ndarray,
    legal_masks: np.ndarray = None,
    *,
    epochs=20,
    batch_size=64,
    lr=0.01,
    seed=0,
    hidden=128,
    preference_pairs=None,
    early_stop_patience=5,
    val_X=None,
    val_y=None,
    val_masks=None,
    on_epoch=None,
) -> ActionModel:
    """Обучение без torch: softmax CE + опциональные пары предпочтений."""
    rng = np.random.default_rng(seed)
    model = ActionModel.random_init(hidden=hidden, seed=seed)
    w1, b1, w2, b2 = model.weights["w1"], model.weights["b1"], model.weights["w2"], model.weights["b2"]
    n = len(X)
    if n == 0:
        raise ValueError("пустой набор данных для обучения")

    best_val = None
    best_snap = None
    patience = 0

    def forward(xb):
        h = np.tanh(xb @ w1 + b1)
        logits = h @ w2 + b2
        return h, logits

    def softmax(logits, masks=None):
        z = logits - logits.max(axis=1, keepdims=True)
        if masks is not None:
            z = np.where(masks.astype(bool), z, -1e9)
        e = np.exp(z)
        return e / (e.sum(axis=1, keepdims=True) + 1e-9)

    for epoch in range(epochs):
        idx = rng.permutation(n)
        total_loss = 0.0
        steps = 0
        for start in range(0, n, batch_size):
            batch = idx[start:start + batch_size]
            xb = X[batch]
            yb = y[batch]
            mb = legal_masks[batch] if legal_masks is not None else None
            h, logits = forward(xb)
            probs = softmax(logits, mb)
            # CE
            rows = np.arange(len(batch))
            loss = -np.log(probs[rows, yb] + 1e-9).mean()
            # grad CE
            dlogits = probs.copy()
            dlogits[rows, yb] -= 1.0
            dlogits /= len(batch)
            dw2 = h.T @ dlogits
            db2 = dlogits.sum(axis=0)
            dh = dlogits @ w2.T
            dh *= (1 - h ** 2)
            dw1 = xb.T @ dh
            db1 = dh.sum(axis=0)
            w2 -= lr * dw2
            b2 -= lr * db2
            w1 -= lr * dw1
            b1 -= lr * db1
            total_loss += float(loss)
            steps += 1

        # Preference pairs: выбранное лучше нежелательного
        if preference_pairs:
            for good, bad, x in preference_pairs:
                h, logits = forward(x[None, :])
                # hinge on score_good - score_bad
                margin = 1.0 - (logits[0, good] - logits[0, bad])
                if margin > 0:
                    d = np.zeros_like(logits)
                    d[0, good] = -1.0
                    d[0, bad] = 1.0
                    dw2 = h.T @ d
                    db2 = d.sum(axis=0)
                    dh = d @ w2.T
                    dh *= (1 - h ** 2)
                    dw1 = x[:, None] @ dh
                    db1 = dh.sum(axis=0)
                    w2 -= lr * dw2
                    b2 -= lr * db2
                    w1 -= lr * 0.1 * dw1  # x is 1d — fix below
            # simpler preference update
            for good, bad, x in preference_pairs[: min(256, len(preference_pairs))]:
                xb = x[None, :]
                h, logits = forward(xb)
                if logits[0, good] - logits[0, bad] >= 1.0:
                    continue
                dlogits = np.zeros((1, ACTION_SPACE), dtype=np.float32)
                dlogits[0, good] = -0.5
                dlogits[0, bad] = 0.5
                dw2 = h.T @ dlogits
                db2 = dlogits.sum(axis=0)
                dh = dlogits @ w2.T * (1 - h ** 2)
                dw1 = xb.T @ dh
                db1 = dh.sum(axis=0)
                w2 -= lr * dw2
                b2 -= lr * db2
                w1 -= lr * dw1
                b1 -= lr * db1

        avg = total_loss / max(steps, 1)
        val_metric = None
        if val_X is not None and len(val_X):
            _, vlogits = forward(val_X)
            vprobs = softmax(vlogits, val_masks)
            pred = vprobs.argmax(axis=1)
            val_metric = float((pred == val_y).mean())
            if best_val is None or val_metric > best_val:
                best_val = val_metric
                best_snap = {
                    "w1": w1.copy(), "b1": b1.copy(),
                    "w2": w2.copy(), "b2": b2.copy(),
                }
                patience = 0
            else:
                patience += 1

        if on_epoch:
            on_epoch(epoch, avg, val_metric)

        if early_stop_patience and patience >= early_stop_patience and best_snap:
            break

    if best_snap:
        w1 = best_snap["w1"]
        b1 = best_snap["b1"]
        w2 = best_snap["w2"]
        b2 = best_snap["b2"]

    model.weights = {"w1": w1, "b1": b1, "w2": w2, "b2": b2}
    model.meta["train"] = {
        "epochs": epochs,
        "batch_size": batch_size,
        "lr": lr,
        "seed": seed,
        "n_examples": int(n),
        "best_val_acc": best_val,
        "backend": "numpy",
    }
    return model
