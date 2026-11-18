"""Структурированный журнал решений (отдельно от frames*.jsonl)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .features import FEATURE_VERSION
from . import paths as _paths

JOURNAL_FORMAT = "1"


def _free_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, ext = path.stem, path.suffix
    n = 1
    while True:
        cand = path.with_name(f"{stem}-{n}{ext}")
        if not cand.exists():
            return cand
        n += 1


def _mask_list(legal_mask) -> list:
    if legal_mask is None:
        return []
    return [int(i) for i in np.flatnonzero(legal_mask)]


def snapshot_state(state, seat: int) -> dict:
    """Только то, что игрок мог знать. Чужие руки — длина, не карты."""
    seats = int(getattr(state, "num_players", 4) or 4)
    hands_view = []
    for s in range(seats):
        cards = list(getattr(state, "hands", [[]])[s] if s < len(state.hands) else [])
        if s == seat:
            hands_view.append({"known": True, "cards": [int(c) for c in cards]})
        else:
            hands_view.append({"known": False, "count": len(cards)})

    known = []
    imposs = []
    kc = getattr(state, "known_cards", None)
    ic = getattr(state, "impossible_cards", None)
    for s in range(min(4, seats)):
        if kc is not None:
            known.append([int(i) for i in np.flatnonzero(kc[s])])
        else:
            known.append([])
        if ic is not None:
            imposs.append([int(i) for i in np.flatnonzero(ic[s])])
        else:
            imposs.append([])

    return {
        "phase": getattr(state, "phase", None),
        "trump": getattr(state, "trump", None),
        "face_up_card": getattr(state, "face_up_card", None),
        "face_up_suit": getattr(state, "face_up_suit", None),
        "bidding_round": getattr(state, "bidding_round", None),
        "declarer": getattr(state, "declarer", None),
        "tricks_played": getattr(state, "tricks_played", 0),
        "current_trick": [[int(s), int(c)] for s, c in (state.current_trick or [])],
        "graveyard": [int(c) for c in (state.graveyard or [])],
        "raw_points_by_team": list(getattr(state, "raw_points_by_team", [0, 0]) or [0, 0]),
        "hands": hands_view,
        "known_cards": known,
        "impossible_cards": imposs,
        "combinations": list(getattr(state, "combinations", ["", "", "", ""]) or []),
        "bot_seats": list(getattr(state, "bot_seats", [False] * 4) or [False] * 4),
        "beliefs_degraded": bool(getattr(state, "beliefs_degraded", False)),
        "num_players": seats,
    }


class DecisionJournal:
    """Пишет JSONL решений. Потокобезопасный append."""

    def __init__(self, path=None, session_id=None):
        _paths.ensure_dirs()
        if path is None:
            path = _paths.JOURNALS_DIR / f"decisions-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        self.path = _free_path(Path(path))
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._f = open(self.path, "a", buffering=1, encoding="utf-8")
        self._pending: dict[str, dict] = {}
        self._decision_seq = 0
        self.match_id: Optional[str] = None
        self.hand_id: Optional[str] = None

    def close(self):
        with self._lock:
            if self._f and not self._f.closed:
                self._f.close()

    def set_match(self, match_id: str):
        self.match_id = match_id

    def set_hand(self, hand_id: str):
        self.hand_id = str(hand_id)

    def begin_decision(
        self,
        *,
        state,
        seat: int,
        match_scores,
        legal_mask,
        source: str,
        agent_name: str = "",
        model_version: str = "",
        mask_origin: str = "state.get_legal_actions",
        game_mode: str = "",
        platform_bot_control: bool = False,
    ) -> str:
        """Создать запись намерения. Возвращает decision_id."""
        self._decision_seq += 1
        did = f"{self.session_id}-{self._decision_seq}"
        seats = int(getattr(state, "num_players", 4) or 4)
        rec = {
            "format": JOURNAL_FORMAT,
            "feature_version": FEATURE_VERSION,
            "decision_id": did,
            "session_id": self.session_id,
            "match_id": self.match_id,
            "hand_id": self.hand_id or getattr(state, "tricks_played", None),
            "t": time.time(),
            "game_mode": game_mode or f"{seats}p",
            "num_players": seats,
            "seat": int(seat),
            "team": int(seat) % 2 if seats == 4 else None,
            "source": source,
            "agent": agent_name,
            "model_version": model_version or None,
            "platform_bot_control": bool(platform_bot_control),
            "phase": getattr(state, "phase", None),
            "state": snapshot_state(state, seat),
            "match_scores_before": list(match_scores or [0, 0]),
            "legal_actions": _mask_list(legal_mask),
            "mask_origin": mask_origin,
            "chosen_action": None,
            "intent_action": None,
            "confirmed_action": None,
            "send_attempts": 0,
            "rejected": False,
            "confirmed": False,
            "compute_ms": None,
            "outcome": {},
            "incomplete": bool(getattr(state, "beliefs_degraded", False)),
            "duplicate_of": None,
        }
        self._pending[did] = rec
        return did

    def record_choice(self, decision_id: str, action: int, compute_ms: float = None):
        rec = self._pending.get(decision_id)
        if not rec:
            return
        rec["intent_action"] = int(action)
        rec["chosen_action"] = int(action)
        if compute_ms is not None:
            rec["compute_ms"] = float(compute_ms)
        rec["send_attempts"] = int(rec.get("send_attempts") or 0) + 1

    def record_reject(self, decision_id: str):
        rec = self._pending.get(decision_id)
        if not rec:
            return
        rec["rejected"] = True

    def record_sent(self, decision_id: str, action: int = None):
        """Отправка выполнена — ещё не подтверждение сервером."""
        rec = self._pending.get(decision_id)
        if not rec:
            return
        if action is not None:
            rec["intent_action"] = int(action)
            rec["chosen_action"] = int(action)
        rec["confirm_status"] = "sent"
        rec["sent"] = True
        rec["confirmed"] = False

    def record_confirm(self, decision_id: str, action: int = None):
        rec = self._pending.get(decision_id)
        if not rec:
            return
        rec["confirmed"] = True
        rec["confirm_status"] = "server_confirmed"
        rec["server_confirmed"] = True
        if action is not None:
            rec["confirmed_action"] = int(action)
            rec["chosen_action"] = int(action)
        self._flush(rec)
        self._pending.pop(decision_id, None)

    def attach_outcome(self, decision_id: str, **fields):
        """Связать с результатом взятки/раздачи (можно после flush — дописать)."""
        rec = self._pending.get(decision_id)
        if rec is not None:
            rec.setdefault("outcome", {}).update(fields)
            return
        # Уже сброшено — дополнительная строка-обновление
        upd = {
            "format": JOURNAL_FORMAT,
            "type": "outcome_update",
            "decision_id": decision_id,
            "t": time.time(),
            "outcome": fields,
        }
        self._write(upd)

    def observe_other(
        self,
        *,
        state,
        seat: int,
        action: int,
        match_scores,
        source: str = "unknown",
    ):
        """Чужой наблюдаемый ход — без выдуманных send/compute/скрытой руки."""
        seats = int(getattr(state, "num_players", 4) or 4)
        rec = {
            "format": JOURNAL_FORMAT,
            "feature_version": FEATURE_VERSION,
            "decision_id": f"{self.session_id}-obs-{uuid.uuid4().hex[:8]}",
            "session_id": self.session_id,
            "match_id": self.match_id,
            "hand_id": self.hand_id,
            "t": time.time(),
            "game_mode": f"{seats}p",
            "num_players": seats,
            "seat": int(seat),
            "team": int(seat) % 2 if seats == 4 else None,
            "source": source,
            "phase": getattr(state, "phase", None),
            "state": snapshot_state(state, seat),
            "match_scores_before": list(match_scores or [0, 0]),
            "legal_actions": None,
            "mask_origin": None,
            "chosen_action": int(action),
            "confirmed_action": int(action),
            "confirmed": True,
            "send_attempts": None,
            "compute_ms": None,
            "observed_only": True,
            "outcome": {},
        }
        self._write(rec)

    def note_empty_mask(self, *, state, seat, reason: str):
        rec = {
            "format": JOURNAL_FORMAT,
            "type": "empty_mask",
            "session_id": self.session_id,
            "match_id": self.match_id,
            "hand_id": self.hand_id,
            "t": time.time(),
            "seat": seat,
            "phase": getattr(state, "phase", None),
            "reason": reason,
        }
        self._write(rec)

    def _flush(self, rec: dict):
        self._write(rec)

    def _write(self, rec: dict):
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            self._f.write(line + "\n")


def iter_journal(path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def merge_outcomes(rows: list[dict]) -> list[dict]:
    """Склеить outcome_update в основные записи."""
    by_id = {}
    extras = []
    for r in rows:
        if r.get("type") == "outcome_update":
            extras.append(r)
            continue
        if r.get("type") == "empty_mask":
            continue
        did = r.get("decision_id")
        if did:
            by_id[did] = r
    for u in extras:
        did = u.get("decision_id")
        if did in by_id:
            by_id[did].setdefault("outcome", {}).update(u.get("outcome") or {})
    return list(by_id.values())
