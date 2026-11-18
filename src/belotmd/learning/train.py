"""Обучение модели на подготовленном датасете (отдельно от игрового процесса)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .model import ActionModel, train_numpy
from . import paths as _paths
from .registry import register_model


def load_split(dataset_dir: Path, part: str):
    path = Path(dataset_dir) / f"{part}.npz"
    if not path.exists():
        return None, None, None
    data = np.load(path)
    return data["X"], data["y"], data["M"]


def run_training(
    dataset_dir,
    *,
    epochs=30,
    batch_size=64,
    lr=0.01,
    seed=0,
    hidden=128,
    early_stop_patience=5,
    model_id=None,
    notes="",
) -> dict:
    _paths.ensure_dirs()
    dataset_dir = Path(dataset_dir)
    X, y, M = load_split(dataset_dir, "train")
    if X is None or len(X) == 0:
        return {
            "ok": False,
            "error": "недостаточно данных для обучения (пустой train)",
            "hint": "Сначала разметьте партии в интерфейсе разбора или "
                    "включите include_unlabeled при подготовке датасета.",
        }

    val = load_split(dataset_dir, "val")
    prefs = None
    pref_path = dataset_dir / "preferences.npz"
    if pref_path.exists():
        p = np.load(pref_path)
        prefs = list(zip(p["good"].tolist(), p["bad"].tolist(), list(p["X"])))

    history = []

    def on_epoch(epoch, loss, val_acc):
        history.append({"epoch": epoch, "loss": loss, "val_acc": val_acc})
        msg = f"[train] epoch {epoch+1}: loss={loss:.4f}"
        if val_acc is not None:
            msg += f" val_acc={val_acc:.3f}"
        print(msg)

    # Проверка: параметры реально меняются
    model0 = ActionModel.random_init(hidden=hidden, seed=seed)
    w0 = model0.weights["w1"].copy()

    model = train_numpy(
        X, y, M,
        epochs=epochs,
        batch_size=batch_size,
        lr=lr,
        seed=seed,
        hidden=hidden,
        preference_pairs=prefs,
        early_stop_patience=early_stop_patience,
        val_X=val[0],
        val_y=val[1],
        val_masks=val[2],
        on_epoch=on_epoch,
    )

    changed = float(np.abs(model.weights["w1"] - w0).mean())
    if changed < 1e-12:
        return {
            "ok": False,
            "error": "параметры модели не изменились — обучение не сработало",
        }

    mid = model_id or f"model-{time.strftime('%Y%m%d-%H%M%S')}"
    model.meta["id"] = mid
    model.meta["dataset"] = str(dataset_dir)
    model.meta["notes"] = notes
    model.meta["metrics"] = {
        "train_size": int(len(X)),
        "val_size": int(len(val[0]) if val[0] is not None else 0),
        "weight_delta_mean": changed,
        "history": history[-5:],
    }

    out = _paths.MODELS_DIR / mid
    model.save(out)
    register_model(model.meta, out)

    report_path = out / "train_report.json"
    report = {
        "ok": True,
        "model_id": mid,
        "path": str(out),
        "changed": True,
        "weight_delta_mean": changed,
        "n_train": int(len(X)),
        "history": history,
        "note": "Модель сохранена, но НЕ активирована автоматически. "
                "Выберите её явно: belot-learn activate " + mid,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"[train] сохранено {out} (ещё не активна)")
    return report


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="belot-learn-train")
    p.add_argument("dataset", help="каталог датасета в data/datasets/")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--lr", type=float, default=0.01)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--hidden", type=int, default=128)
    p.add_argument("--id", dest="model_id", default=None)
    args = p.parse_args(argv)
    ds = Path(args.dataset)
    if not ds.is_absolute():
        ds = _paths.DATASETS_DIR / args.dataset
    result = run_training(
        ds,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        seed=args.seed,
        hidden=args.hidden,
        model_id=args.model_id,
    )
    if not result.get("ok"):
        print(result.get("error") or result)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
