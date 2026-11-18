"""Состояние строго на момент решения (до выбора действия).

Общий механизм для анализатора, сборщика отчёта и ручных примеров.
Не подмешивает будущие события после t_decision.
"""

from __future__ import annotations

from typing import Any


def first_defined(*vals: Any) -> Any:
    for v in vals:
        if v is not None:
            return v
    return None


def _before(ev: dict, t_decision: float | None, line_decision: int | None) -> bool:
    """Событие произошло до выбора (не после)."""
    if t_decision is not None and isinstance(ev.get("t"), (int, float)):
        return float(ev["t"]) <= float(t_decision) + 1e-9
    if line_decision is not None and isinstance(ev.get("_srcLine"), int):
        return int(ev["_srcLine"]) <= int(line_decision)
    # порядок неизвестен — не утверждаем «до»
    return False


def seat_hand_from_state(state: dict | None, seat: Any) -> dict:
    out = {
        "cards": None,
        "known": None,
        "complete": None,
        "count": None,
        "source": None,
    }
    if not isinstance(state, dict) or seat is None:
        return out
    hands = state.get("hands")
    if not isinstance(hands, list):
        return out
    try:
        si = int(seat)
    except (TypeError, ValueError):
        return out
    if not (0 <= si < len(hands)):
        return out
    h = hands[si]
    if not isinstance(h, dict):
        return out
    known = h.get("known")
    cards = h.get("cards") if isinstance(h.get("cards"), list) else None
    count = first_defined(h.get("count"), len(cards) if cards is not None else None)
    out.update({
        "cards": cards,
        "known": known,
        "count": count,
        "complete": True if known is True and cards is not None else (False if known is False else None),
        "source": "decision.state.hands[seat]",
    })
    return out


def recover_trump_at_decision(
    *,
    events: list[dict],
    hand_id: Any,
    t_decision: float | None = None,
    line_decision: int | None = None,
    decision_state: dict | None = None,
) -> dict:
    """trump/declarer/trumpOpen только из state решения или предшествующих событий."""
    result = {
        "trump": None,
        "declarer": None,
        "trumpOpen": None,
        "source": None,
        "knownAtDecision": False,
        "unknown": True,
    }
    st = decision_state if isinstance(decision_state, dict) else {}
    if st.get("trump") is not None:
        result["trump"] = st.get("trump")
        result["declarer"] = st.get("declarer")
        # не подставляем False/True по умолчанию
        if "trumpOpen" in st:
            result["trumpOpen"] = st.get("trumpOpen")
        elif "declarer_has_played_trump" in st:
            result["trumpOpen"] = bool(st.get("declarer_has_played_trump"))
        result["source"] = "decision.state"
        result["knownAtDecision"] = True
        result["unknown"] = False
        return result

    # последнее предшествующее событие той же раздачи
    best = None
    for ev in events:
        if ev.get("type") not in ("trump", "hand_start"):
            continue
        if str(ev.get("handId")) != str(hand_id):
            continue
        if not _before(ev, t_decision, line_decision):
            continue
        if ev.get("trump") is None and ev.get("type") != "hand_start":
            continue
        best = ev
    if best is not None and best.get("trump") is not None:
        result["trump"] = best.get("trump")
        result["declarer"] = best.get("declarer")
        if "trumpOpen" in best:
            result["trumpOpen"] = best.get("trumpOpen")
        result["source"] = f"event:{best.get('type')} line={best.get('_srcLine')}"
        result["knownAtDecision"] = True
        result["unknown"] = False
    return result


def build_state_at_decision(
    *,
    decision: dict,
    events: list[dict],
) -> dict:
    """Полное состояние, доступное боту в момент выбора."""
    state = decision.get("state") if isinstance(decision.get("state"), dict) else {}
    seat = decision.get("seat")
    hand_id = decision.get("handId")
    t_dec = decision.get("t")
    # для merged decisions — время первой записи (chosen)
    stages = decision.get("stages") or []
    if stages and isinstance(stages[0], dict) and stages[0].get("t") is not None:
        t_dec = stages[0].get("t")
    line_dec = None
    src_lines = decision.get("sourceLines") or []
    if src_lines:
        try:
            line_dec = int(min(x for x in src_lines if isinstance(x, int)))
        except ValueError:
            line_dec = None

    hand = seat_hand_from_state(state, seat)
    trump = recover_trump_at_decision(
        events=events,
        hand_id=hand_id,
        t_decision=float(t_dec) if isinstance(t_dec, (int, float)) else None,
        line_decision=line_dec,
        decision_state=state,
    )
    table = state.get("current_trick")
    if table is None:
        table = decision.get("tableBefore")
    return {
        "handId": hand_id,
        "seat": seat,
        "hand": hand,
        "table": table,
        "trump": trump,
        "graveyard": state.get("graveyard"),
        "combinations": state.get("combinations"),
        "scores": first_defined(
            decision.get("matchScoresBefore"),
            state.get("raw_points_by_team"),
        ),
        "legalActions": first_defined(
            decision.get("legalActions"),
            decision.get("legal_actions"),
        ),
        "phase": first_defined(state.get("phase"), decision.get("phase")),
        "numPlayers": state.get("num_players"),
        "biddingRound": state.get("bidding_round"),
        "sources": {
            "state": "decision.state" if state else None,
            "trump": trump.get("source"),
            "hand": hand.get("source"),
            "tDecision": t_dec,
            "lineDecision": line_dec,
        },
    }
