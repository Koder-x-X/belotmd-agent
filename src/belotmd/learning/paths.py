"""Каталоги данных относительно корня проекта belotmd_agent."""

from pathlib import Path

from ..config import PROJECT_ROOT

DATA_ROOT = PROJECT_ROOT / "data"
JOURNALS_DIR = DATA_ROOT / "journals"
ANNOTATIONS_DIR = DATA_ROOT / "annotations"
DATASETS_DIR = DATA_ROOT / "datasets"
MODELS_DIR = PROJECT_ROOT / "models"
ACTIVE_FILE = MODELS_DIR / "active.json"
REGISTRY_FILE = MODELS_DIR / "registry.json"


def ensure_dirs():
    for d in (JOURNALS_DIR, ANNOTATIONS_DIR, DATASETS_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
