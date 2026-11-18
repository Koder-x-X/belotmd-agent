"""Нормализованный ключ ситуации — точное сопоставление, без «похожести»."""

from __future__ import annotations

import hashlib
import json
from typing import Any


KEY_VERSION = "exact-3"


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canon(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return round(float(value), 6)
    if value is None:
        return None
    return str(value)


def situation_payload(rec: dict) -> dict:
    """Поля, влияющие на решение (ТЗ §8). Чужие закрытые руки не входят.

    Не подставляет []/None вместо известных данных. Неполная рука → hand=None
    (вызывающий не должен сливать с полными ключами).
    """
    state = rec.get("state") if isinstance(rec.get("state"), dict) else {}
    hand = None
    hands = state.get("hands") or []
    seat = rec.get("seat")
    try:
        seat = int(seat) if seat is not None else None
    except (TypeError, ValueError):
        seat = None
    if isinstance(hands, list) and isinstance(seat, int) and 0 <= seat < len(hands):
        mine = hands[seat]
        if isinstance(mine, dict) and mine.get("known"):
            cards = mine.get("cards")
            if isinstance(cards, list):
                hand = sorted(int(c) for c in cards)
    if "current_trick" in state:
        trick = state.get("current_trick")
    elif "trick" in rec:
        trick = rec.get("trick")
    else:
        trick = None
    trick_norm = [[int(s), int(c)] for s, c in trick] if isinstance(trick, list) else None
    legal = rec.get("legal_actions")
    if legal is None:
        legal = rec.get("legalActions")
    legal_norm = sorted(int(a) for a in legal) if isinstance(legal, list) else None
    scores = rec.get("match_scores_before")
    if scores is None:
        scores = state.get("raw_points_by_team")
    scores_norm = list(scores) if isinstance(scores, list) else None
    combos = state.get("combinations")
    hand_complete = bool(rec.get("hand_complete"))
    if hand is not None and rec.get("hand_complete") is None:
        # явная полнота не передана — не утверждаем complete
        hand_complete = False
    # отсутствие legalTrusted ≠ «достоверно»
    legal_trusted = rec.get("legal_trusted")
    if legal_trusted is None and "legalTrusted" in rec:
        legal_trusted = rec.get("legalTrusted")
    # различать «известно пусто» (list) и «нет данных» (None)
    if "graveyard" in state:
        grave = state.get("graveyard")
        grave_norm = sorted(int(c) for c in grave) if isinstance(grave, list) else None
    else:
        grave_norm = None
    if "combinations" in state:
        combos_norm = list(combos) if isinstance(combos, list) else None
    else:
        combos_norm = None
    trump_open = state.get("trumpOpen", state.get("trump_open", rec.get("trumpOpen")))
    if trump_open is None and "declarer_has_played_trump" in state:
        trump_open = bool(state.get("declarer_has_played_trump"))
    return {
        "key_version": KEY_VERSION,
        "game_mode": rec.get("game_mode") or f"{rec.get('num_players') or state.get('num_players') or '?'}p",
        "rules_version": rec.get("rules_version") or state.get("rules_version"),
        "phase": state.get("phase", rec.get("phase")),
        "trump": state.get("trump", rec.get("trump")),
        "seat": seat,
        "trick_len": len(trick_norm) if trick_norm is not None else None,
        "trick": trick_norm,
        "hand": hand,
        "hand_complete": hand_complete if hand is not None else None,
        "legal_actions": legal_norm,
        "legal_trusted": legal_trusted if legal_norm is not None else None,
        "graveyard": grave_norm,
        "declarer": state.get("declarer", rec.get("declarer")),
        "trump_open": trump_open,
        "tricks_played": state.get("tricks_played", rec.get("tricks_played")),
        "match_scores": scores_norm,
        "combinations": combos_norm,
        "face_up_card": state.get("face_up_card"),
        "bidding_round": state.get("bidding_round"),
        "contract": state.get("contract", rec.get("contract")),
        "participation_id": rec.get("participation_id") or rec.get("gameId"),
        "match_id": rec.get("match_id") or rec.get("gameId"),
    }


def situation_key(rec: dict) -> str:
    payload = _canon(situation_payload(rec))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
