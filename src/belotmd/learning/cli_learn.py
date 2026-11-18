"""Единая CLI для записи/датасета/обучения/моделей: belot-learn."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="belot-learn",
        description="Подготовка данных, обучение и управление моделями belotmd_agent",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-frames", help="Импорт frames*.jsonl в журнал решений")
    s.add_argument("frames")
    s.add_argument("-o", "--out", default=None)

    s = sub.add_parser("prepare", help="Собрать датасет из журналов и разметки")
    s.add_argument("--journals", nargs="*", default=None)
    s.add_argument("--include-unlabeled", action="store_true")
    s.add_argument("--name", default=None)
    s.add_argument("--seed", type=int, default=0)

    s = sub.add_parser("train", help="Обучить модель на датасете")
    s.add_argument("dataset")
    s.add_argument("--epochs", type=int, default=30)
    s.add_argument("--batch-size", type=int, default=64)
    s.add_argument("--lr", type=float, default=0.01)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--hidden", type=int, default=128)
    s.add_argument("--id", dest="model_id", default=None)

    s = sub.add_parser("eval", help="Проверить модель на hold-out")
    s.add_argument("model")
    s.add_argument("dataset")
    s.add_argument("--part", default="test")

    s = sub.add_parser("models", help="Список моделей")
    s = sub.add_parser("activate", help="Сделать модель активной")
    s.add_argument("model_id")
    s = sub.add_parser("rollback", help="Откатить активную модель")
    s = sub.add_parser("active", help="Показать активную модель")

    s = sub.add_parser("review", help="Локальный интерфейс разбора (127.0.0.1)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8791)

    args = p.parse_args(argv)

    if args.cmd == "import-frames":
        from .import_frames import import_frames
        print(json.dumps(import_frames(args.frames, args.out),
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "prepare":
        from .dataset import build_dataset, save_dataset_bundle
        from . import paths as _paths
        paths = args.journals
        if paths:
            paths = [Path(x) for x in paths]
        bundle = build_dataset(
            paths,
            include_unlabeled=args.include_unlabeled,
            seed=args.seed,
        )
        print(json.dumps(bundle["report"], ensure_ascii=False, indent=2))
        if bundle["report"]["n_examples"] == 0:
            print("Качественных примеров нет — датасет не сохранён.", file=sys.stderr)
            return 2
        out = save_dataset_bundle(bundle, name=args.name)
        print(f"saved: {out}")
        return 0

    if args.cmd == "train":
        from .train import run_training
        from . import paths as _paths
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
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.cmd == "eval":
        from .evaluate import evaluate_model
        from . import paths as _paths
        ds = Path(args.dataset)
        if not ds.is_absolute():
            ds = _paths.DATASETS_DIR / args.dataset
        mp = Path(args.model)
        if not mp.is_absolute():
            mp = _paths.MODELS_DIR / args.model
        result = evaluate_model(mp, ds, part=args.part)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.cmd == "models":
        from .registry import list_models, active_info
        active = active_info()
        for m in list_models():
            mark = " *" if m.get("id") == active.get("id") else ""
            print(f"{m['id']}{mark}\t{m.get('path')}")
        return 0

    if args.cmd == "activate":
        from .registry import activate_model
        info = activate_model(args.model_id)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        print("Смена модели применяется со следующего матча бота.")
        return 0

    if args.cmd == "rollback":
        from .registry import rollback_model
        info = rollback_model()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "active":
        from .registry import active_info
        print(json.dumps(active_info(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "review":
        from ..review.server import run_server
        run_server(host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
