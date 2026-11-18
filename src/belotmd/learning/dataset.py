"""Подготовка учебных данных из журналов и пользовательской разметки."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from .features import FEATURE_VERSION, encode_state
from .journal import iter_journal, merge_outcomes
from . import paths as _paths
from ..game.state import BelotState


def load_annotations(path=None) -> dict:
    _paths.ensure_dirs()
    path = Path(path or (_paths.ANNOTATIONS_DIR / "annotations.json"))
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_annotation(decision_id: str, annotation: dict, path=None):
    _paths.ensure_dirs()
    path = Path(path or (_paths.ANNOTATIONS_DIR / "annotations.json"))
    data = load_annotations(path)
    annotation = dict(annotation)
    annotation["updated_at"] = time.time()
    annotation.setdefault("version", 1)
    data[decision_id] = annotation
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return annotation


def _state_from_snapshot(snap: dict, seat: int) -> BelotState:
    """Восстановить BelotState для encode (без чужих реальных рук)."""
    st = BelotState()
    seats = int(snap.get("num_players") or 4)
    st.num_players = seats
    st.phase = snap.get("phase") or "PLAYING"
    st.trump = snap.get("trump")
    st.face_up_card = snap.get("face_up_card")
    st.face_up_suit = snap.get("face_up_suit")
    st.bidding_round = snap.get("bidding_round") or 1
    st.declarer = snap.get("declarer")
    st.tricks_played = snap.get("tricks_played") or 0
    st.current_trick = [tuple(x) for x in (snap.get("current_trick") or [])]
    st.graveyard = list(snap.get("graveyard") or [])
    st.raw_points_by_team = list(snap.get("raw_points_by_team") or [0, 0])
    st.combinations = list(snap.get("combinations") or ["", "", "", ""])
    st.bot_seats = list(snap.get("bot_seats") or [False] * 4)
    st.beliefs_degraded = bool(snap.get("beliefs_degraded"))
    st.hands = [[] for _ in range(max(seats, 4))]
    for s, hv in enumerate(snap.get("hands") or []):
        if s >= len(st.hands):
            break
        if hv.get("known"):
            st.hands[s] = list(hv.get("cards") or [])
        else:
            # Заглушки длины — encode их не читает
            st.hands[s] = [-1] * int(hv.get("count") or 0)
    st.known_cards = np.zeros((4, 32), dtype=bool)
    st.impossible_cards = np.zeros((4, 32), dtype=bool)
    for s, cards in enumerate(snap.get("known_cards") or []):
        for c in cards:
            if 0 <= int(c) < 32:
                st.known_cards[s, int(c)] = True
    for s, cards in enumerate(snap.get("impossible_cards") or []):
        for c in cards:
            if 0 <= int(c) < 32:
                st.impossible_cards[s, int(c)] = True
    st.current_player = seat
    return st


def _target_from_annotation(rec: dict, ann: dict | None) -> tuple[int | None, str]:
    """Вернуть (target_action, reason) или (None, exclude_reason)."""
    if ann and ann.get("exclude"):
        return None, "excluded"
    legal = set(rec.get("legal_actions") or [])
    chosen = rec.get("confirmed_action")
    if chosen is None:
        chosen = rec.get("chosen_action")

    if ann:
        preferred = ann.get("preferred_action")
        acceptable = ann.get("acceptable_actions") or []
        label = ann.get("label")  # good | bad | None

        if preferred is not None:
            preferred = int(preferred)
            if legal and preferred not in legal:
                return None, "preferred_not_legal"
            return preferred, "preferred"

        if acceptable:
            ok = [int(a) for a in acceptable if not legal or int(a) in legal]
            if not ok:
                return None, "acceptable_not_legal"
            # Несколько приемлемых — берём фактический, если он среди них
            if chosen is not None and int(chosen) in ok:
                return int(chosen), "acceptable_includes_chosen"
            return ok[0], "acceptable_first"

        if label == "good" and chosen is not None:
            return int(chosen), "good_confirms_chosen"

        if label == "bad" and preferred is None and not acceptable:
            return None, "bad_without_fix"

    # Неразмеченные — только по флагу include_unlabeled
    return None, "unlabeled"


def build_dataset(
    journal_paths=None,
    *,
    include_unlabeled: bool = False,
    include_sources: tuple = ("human", "our_agent", "strong", "heuristic"),
    exclude_sources: tuple = ("random",),
    annotations_path=None,
    seed: int = 0,
) -> dict:
    _paths.ensure_dirs()
    if journal_paths is None:
        journal_paths = sorted(_paths.JOURNALS_DIR.glob("*.jsonl"))
    else:
        journal_paths = [Path(p) for p in journal_paths]

    anns = load_annotations(annotations_path)
    excluded = []
    examples = []
    preference_pairs = []

    for jp in journal_paths:
        if not jp.exists():
            continue
        rows = merge_outcomes(iter_journal(jp))
        for rec in rows:
            if rec.get("observed_only"):
                excluded.append({"id": rec.get("decision_id"), "reason": "observed_only"})
                continue
            if rec.get("incomplete"):
                excluded.append({"id": rec.get("decision_id"), "reason": "incomplete"})
                continue
            if not rec.get("confirmed") and rec.get("confirmed_action") is None:
                # Намерение без подтверждения — не учебный пример
                if rec.get("chosen_action") is None:
                    excluded.append({"id": rec.get("decision_id"), "reason": "unconfirmed"})
                    continue
            src = (rec.get("source") or "unknown").lower()
            agent = (rec.get("agent") or "").lower()
            if any(x in src or x == agent for x in exclude_sources):
                # random / наш бот без разметки — не тащим ошибки
                if rec.get("decision_id") not in anns:
                    excluded.append({"id": rec.get("decision_id"), "reason": f"source:{src}"})
                    continue

            ann = anns.get(rec.get("decision_id") or "")
            target, reason = _target_from_annotation(rec, ann)
            if target is None:
                if include_unlabeled and reason == "unlabeled":
                    chosen = rec.get("confirmed_action") or rec.get("chosen_action")
                    if chosen is None:
                        excluded.append({"id": rec.get("decision_id"), "reason": "no_action"})
                        continue
                    # Только размеченные источники качества
                    if src not in include_sources and agent not in include_sources:
                        excluded.append({"id": rec.get("decision_id"), "reason": "unlabeled_filtered"})
                        continue
                    target = int(chosen)
                    reason = "unlabeled_included"
                else:
                    excluded.append({"id": rec.get("decision_id"), "reason": reason})
                    continue

            legal = rec.get("legal_actions") or []
            if legal and int(target) not in set(legal):
                excluded.append({"id": rec.get("decision_id"), "reason": "target_illegal"})
                continue

            snap = rec.get("state") or {}
            seat = int(rec.get("seat", 0))
            try:
                st = _state_from_snapshot(snap, seat)
                mask = np.zeros(38, dtype=np.int8)
                for a in legal:
                    if 0 <= int(a) < 38:
                        mask[int(a)] = 1
                if not mask.any() and legal is not None:
                    # восстановить по правилам, пометить происхождение
                    mask = st.get_legal_actions().astype(np.int8)
                    mask_origin = "reconstructed_rules"
                else:
                    mask_origin = rec.get("mask_origin") or "journal"
                x = encode_state(st, seat, rec.get("match_scores_before") or [0, 0], mask)
            except Exception as exc:
                excluded.append({
                    "id": rec.get("decision_id"),
                    "reason": f"encode_error:{type(exc).__name__}",
                })
                continue

            # Future leak guard: features must not depend on outcome
            if "future" in (rec.get("outcome") or {}) and False:
                pass

            ex = {
                "decision_id": rec.get("decision_id"),
                "match_id": rec.get("match_id"),
                "hand_id": rec.get("hand_id"),
                "session_id": rec.get("session_id"),
                "target": int(target),
                "reason": reason,
                "mask_origin": mask_origin,
                "x": x,
                "mask": mask,
                "observed": True,
                "restored": mask_origin == "reconstructed_rules",
            }
            examples.append(ex)

            # Пары предпочтений из разметки
            if ann and ann.get("label") == "bad" and ann.get("preferred_action") is not None:
                bad = rec.get("chosen_action")
                good = int(ann["preferred_action"])
                if bad is not None and int(bad) != good:
                    preference_pairs.append((good, int(bad), x))

    report = {
        "feature_version": FEATURE_VERSION,
        "n_examples": len(examples),
        "n_excluded": len(excluded),
        "excluded_reasons": _count_reasons(excluded),
        "n_preference_pairs": len(preference_pairs),
        "journals": [str(p) for p in journal_paths],
        "built_at": time.time(),
    }
    return {
        "examples": examples,
        "preference_pairs": preference_pairs,
        "excluded": excluded,
        "report": report,
    }


def _count_reasons(excluded):
    counts = {}
    for e in excluded:
        r = e.get("reason") or "?"
        counts[r] = counts.get(r, 0) + 1
    return counts


def split_by_match(examples: list, *, seed=0, train=0.7, val=0.15) -> dict:
    """Деление целыми матчами/сессиями — без пересечения раздач."""
    rng = np.random.default_rng(seed)
    groups = {}
    for ex in examples:
        key = ex.get("match_id") or ex.get("session_id") or ex.get("hand_id") or ex["decision_id"]
        groups.setdefault(str(key), []).append(ex)
    keys = list(groups.keys())
    rng.shuffle(keys)
    n = len(keys)
    if n == 0:
        return {"train": [], "val": [], "test": [], "group_counts": {"train": 0, "val": 0, "test": 0}}
    # Мало групп: всё в train, иначе обучение получит пустой набор.
    if n == 1:
        train_k, val_k, test_k = set(keys), set(), set()
    elif n == 2:
        train_k, val_k, test_k = {keys[0]}, {keys[1]}, set()
    else:
        n_train = max(1, int(n * train))
        n_val = max(1, int(n * val)) if n >= 3 else 0
        if n_train + n_val >= n:
            n_val = max(0, n - n_train - 1)
        train_k = set(keys[:n_train])
        val_k = set(keys[n_train:n_train + n_val])
        test_k = set(keys[n_train + n_val:])

    def take(ks):
        out = []
        for k in ks:
            out.extend(groups[k])
        return out

    return {
        "train": take(train_k),
        "val": take(val_k),
        "test": take(test_k),
        "group_counts": {
            "train": len(train_k), "val": len(val_k), "test": len(test_k),
        },
    }


def examples_to_arrays(examples: list) -> tuple:
    if not examples:
        return (
            np.zeros((0, 1), dtype=np.float32),
            np.zeros((0,), dtype=np.int64),
            np.zeros((0, 38), dtype=np.int8),
        )
    X = np.stack([e["x"] for e in examples]).astype(np.float32)
    y = np.array([e["target"] for e in examples], dtype=np.int64)
    M = np.stack([e["mask"] for e in examples]).astype(np.int8)
    return X, y, M


def save_dataset_bundle(bundle: dict, name: str = None) -> Path:
    _paths.ensure_dirs()
    name = name or f"dataset-{time.strftime('%Y%m%d-%H%M%S')}"
    out = _paths.DATASETS_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    split = split_by_match(bundle["examples"])
    for part in ("train", "val", "test"):
        X, y, M = examples_to_arrays(split[part])
        np.savez_compressed(out / f"{part}.npz", X=X, y=y, M=M)
    (out / "report.json").write_text(
        json.dumps({**bundle["report"], "split": split["group_counts"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "excluded.json").write_text(
        json.dumps(bundle["excluded"][:5000], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # preference pairs — только индексы целей и x
    if bundle["preference_pairs"]:
        goods = np.array([p[0] for p in bundle["preference_pairs"]], dtype=np.int64)
        bads = np.array([p[1] for p in bundle["preference_pairs"]], dtype=np.int64)
        Xs = np.stack([p[2] for p in bundle["preference_pairs"]]).astype(np.float32)
        np.savez_compressed(out / "preferences.npz", good=goods, bad=bads, X=Xs)
    return out
