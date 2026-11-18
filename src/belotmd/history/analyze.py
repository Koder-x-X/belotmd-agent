"""Две оценки хода: at_time и post_reveal.

rules-2.1 (после независимой проверки):
- связь decision↔play по (handId, seat, card/action), не только по карте;
- рука перед ходом только из последовательного handBefore, без подстановки initialHand;
- is_last только при полной взятке (seats ходов);
- legal соперника через правила BelotState, не только follow-suit;
- без авто-ошибки mid-trick.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from belotmd.agents.strong_agent import (
    _NT_POWER,
    _T_POWER,
    _beats,
    _current_winner_card,
    card_points as _engine_card_points,
)

ANALYZER_VERSION = "rules-2.2"

# Идентификаторы, которые считаем спец-правилами Fallen (не Strong fallback)
_FALLEN_RULE_IDS = frozenset({
    "FALLEN", "EXP", "EXP_LAST", "KEEP_TRUMP", "CHEAP_WIN", "SAVE_JACK",
    "TRUMP_FORCE", "FOLLOW", "VOID", "LEAD",
})
EVAL_LABELS = (
    "удачный",
    "неудачный",
    "спорный",
    "неоценённый",
    "возможная_ошибка",
    "кандидат_на_разбор",
    "подтверждённый_факт",
    "соответствует_fallen",
)


def card_points(card: int, trump: int | None) -> int:
    return int(_engine_card_points(int(card), trump))


def trick_power(card: int, trump: int | None, led_suit: int | None) -> tuple:
    suit = int(card) // 8
    rank = int(card) % 8
    if trump is not None and suit == int(trump):
        return (2, _T_POWER[rank])
    if led_suit is not None and suit == int(led_suit):
        return (1, _NT_POWER[rank])
    return (0, 0)


def _engine_legal_cards(
    hand: list[int],
    table: list[tuple[int, int]],
    trump: int | None,
    *,
    seats: int = 3,
    seat: int = 0,
    declarer: int | None = None,
    trump_open: bool = True,
) -> tuple[list[int] | None, list[str]]:
    """Допустимые карты через BelotState.get_legal_actions. None = недостоверно."""
    limits: list[str] = []
    if not hand:
        return None, ["нет руки для legal"]
    try:
        from belotmd.game.state import BelotState
    except Exception as exc:
        return None, [f"движок недоступен: {exc}"]
    try:
        st = BelotState()
        st.num_players = int(seats)
        st.phase = "PLAYING"
        st.trump = trump
        st.declarer = int(declarer) if declarer is not None else seat
        st.declarer_has_played_trump = bool(trump_open)
        st.current_player = int(seat)
        st.current_trick = [(int(s), int(c)) for s, c in table]
        st.hands = [[] for _ in range(int(seats))]
        if 0 <= seat < len(st.hands):
            st.hands[seat] = list(hand)
        mask = st.get_legal_actions()
        allowed = [c for c in hand if 0 <= c < len(mask) and bool(mask[c])]
        if not allowed and hand:
            limits.append("движок вернул пустой legal при непустой руке")
            return None, limits
        return allowed, limits
    except Exception as exc:
        return None, [f"legal engine error: {exc}"]


def _legal_from_hand(
    hand: list[int],
    table: list[tuple[int, int]],
    trump: int | None,
    legal_mask: list[int] | None = None,
    *,
    seats: int = 3,
    seat: int = 0,
    declarer: int | None = None,
    trump_open: bool = True,
    require_engine: bool = False,
) -> tuple[list[int], list[str], bool]:
    """Возвращает (legal, limits, trusted). trusted=False → не «подтверждённая ошибка»."""
    limits: list[str] = []
    if legal_mask is not None:
        allowed = {int(a) for a in legal_mask if 0 <= int(a) <= 31}
        out = [c for c in hand if c in allowed]
        return out, limits, True

    eng, eng_limits = _engine_legal_cards(
        hand, table, trump,
        seats=seats, seat=seat, declarer=declarer, trump_open=trump_open,
    )
    limits.extend(eng_limits)
    if eng is not None:
        return eng, limits, True
    if require_engine:
        return [], limits + ["legal недостоверен"], False
    # fallback follow-suit — не trusted
    if not table:
        return list(hand), limits + ["legal: fallback lead-any"], False
    led = table[0][1] // 8
    follow = [c for c in hand if c // 8 == led]
    if follow:
        return follow, limits + ["legal: fallback follow-suit"], False
    return list(hand), limits + ["legal: fallback void"], False


def _beats_table(card: int, table: list[tuple[int, int]], trump: int | None) -> bool:
    if not table:
        return True
    led = table[0][1] // 8
    current = _current_winner_card(table, trump)
    return _beats(card, current, trump, led)


def _cheaper_winners(
    hand: list[int],
    table: list[tuple[int, int]],
    played: int,
    trump: int | None,
    legal: list[int],
) -> list[int]:
    if played not in legal or not _beats_table(played, table, trump):
        return []
    cost = card_points(played, trump)
    out = []
    for c in legal:
        if c == played:
            continue
        if _beats_table(c, table, trump) and card_points(c, trump) < cost:
            out.append(c)
    return out


def evaluate_play(
    *,
    seat: int,
    card: int,
    hand_before: list[int] | None,
    table_before: list[tuple[int, int]],
    trump: int | None,
    won: bool | None,
    trick_points: int | None,
    legal_actions: list[int] | None = None,
    fallen_rule_id: str | None = None,
    mode: str = "at_time",
    is_last: bool = False,
    seats: int = 3,
    declarer: int | None = None,
    trump_open: bool | None = None,
    legal_trusted: bool = True,
    rule_source: str | None = None,
    trump_unknown: bool = False,
) -> dict:
    fact = {
        "wonTrick": won,
        "trickPoints": trick_points,
        "card": card,
        # trump=None без trump_unknown — режим без козыря (NT), очки считаем
        "cardPoints": None if trump_unknown else card_points(card, trump),
        "isLastToTrick": bool(is_last),
    }
    label = "неоценённый"
    category = "недостаточно_данных"
    confidence = 0.0
    explanation = "недостаточно данных для правил analyzer " + ANALYZER_VERSION
    alternatives: list[int] = []
    limits: list[str] = []

    # неизвестный козырь (нет данных) ≠ режим без козыря
    if trump_unknown:
        limits.append("козырь неизвестен на момент оценки")
        legal_trusted = False

    hand = list(hand_before or [])
    if not hand:
        out = {
            "mode": mode,
            "label": "неоценённый",
            "category": "недостаточно_данных",
            "confidence": 0.0,
            "explanation": "рука перед ходом неизвестна (без подстановки стартовой руки)",
            "method": ANALYZER_VERSION,
            "fact": fact,
            "alternatives": [],
            "limits": ["нет руки перед ходом"],
            "legalTrusted": False,
            "trumpKnown": (not trump_unknown),
        }
        rid = str(fallen_rule_id or "")
        if rid and rid.upper() not in ("STRONG", "STRONG2", "BID", "HEURISTIC") and (
            rid.upper() in _FALLEN_RULE_IDS or rid.upper().startswith("FALLEN") or rid.upper().startswith("EXP")
        ):
            out["fallenRule"] = {
                "ruleId": fallen_rule_id,
                "note": "соответствует правилу Fallen — не доказательство лучшего хода",
            }
        elif fallen_rule_id:
            out["strategyMatch"] = {
                "ruleId": fallen_rule_id,
                "source": "base_strategy",
                "note": "не специальная тактика Fallen",
            }
        return out

    engine_ok = (not trump_unknown) or not table_before
    legal, legal_limits, trusted = _legal_from_hand(
        hand, table_before, trump, legal_actions,
        seats=seats, seat=seat, declarer=declarer,
        trump_open=bool(trump_open) if trump_open is not None else True,
        require_engine=(legal_actions is None and engine_ok),
    )
    trusted = bool(trusted and legal_trusted and not trump_unknown)
    if trump_unknown:
        trusted = False
        limits.append("legalTrusted=false: козырь неизвестен")
    if trump_open is None and not trump_unknown:
        limits.append("trumpOpen неизвестен")
    limits.extend(legal_limits)
    if card not in legal and card in hand:
        limits.append("сыгранная карта вне восстановленного legal — данные неполны")
        legal = list(dict.fromkeys(list(legal) + [card]))
        trusted = False

    beaters = [c for c in legal if _beats_table(c, table_before, trump)]
    cheaper = _cheaper_winners(hand, table_before, card, trump, legal)
    alternatives = list(cheaper)

    if won is True:
        fact_note = "наблюдение: взятка взята"
    elif won is False:
        fact_note = "наблюдение: взятка отдана"
    else:
        fact_note = "наблюдение: итог взятки неизвестен"

    def _downgrade_untrusted(lab: str, cat: str, conf: float, expl: str, alts: list[int]):
        if trusted:
            return lab, cat, conf, expl, alts
        return (
            "кандидат_на_разбор",
            "кандидат_на_разбор",
            min(conf, 0.25),
            expl + "; legal недостоверен — не подтверждённая ошибка",
            alts,
        )

    is_fallen_rule = bool(
        fallen_rule_id
        and (
            str(fallen_rule_id).upper() in _FALLEN_RULE_IDS
            or str(fallen_rule_id).upper().startswith("FALLEN")
            or str(fallen_rule_id).upper().startswith("EXP")
        )
        and str(fallen_rule_id).upper() not in ("STRONG", "STRONG2", "BID", "HEURISTIC")
    )
    strategy_note = None
    if fallen_rule_id and mode == "at_time":
        if is_fallen_rule:
            label = "соответствует_fallen"
            category = "соответствует_fallen"
            confidence = 0.3
            explanation = (
                f"{fact_note}; сработало правило Fallen {fallen_rule_id} "
                f"— не доказательство лучшего хода"
            )
            strategy_note = {"ruleId": fallen_rule_id, "source": "fallen"}
        else:
            # STRONG и прочие — соответствие базовой стратегии, не спец-тактика Fallen
            strategy_note = {
                "ruleId": fallen_rule_id,
                "source": rule_source or "base_strategy",
                "note": "не специальная тактика Fallen",
            }
            # дальше оцениваем качество обычным путём
            fallen_rule_id = None

    if fallen_rule_id and mode == "at_time" and is_fallen_rule:
        pass  # уже выставили соответствует_fallen
    elif not is_last:
        if card in beaters and won is False:
            label, category, confidence = "кандидат_на_разбор", "кандидат_на_разбор", 0.25
            alternatives = [c for c in beaters if c != card][:8]
            explanation = (
                f"{fact_note}; ход не последний — карта могла бить стол, "
                f"но победа не гарантирована; авто-ошибку не ставим"
            )
            limits.append("не последний во взятке")
        elif cheaper and won is True:
            label, category, confidence = "кандидат_на_разбор", "кандидат_на_разбор", 0.25
            alternatives = cheaper
            explanation = (
                f"{fact_note}; были более дешёвые бьющие, но не последний ход "
                f"— переплату не считаем доказанной"
            )
            limits.append("не последний во взятке")
        else:
            label, category, confidence = "спорный", "спорный_ход", 0.2
            explanation = f"{fact_note}; промежуточный ход — оценка ограничена"
            limits.append("не последний во взятке")
    else:
        if won is True and cheaper:
            label, category, confidence, explanation, alternatives = _downgrade_untrusted(
                "возможная_ошибка",
                "возможная_ошибка",
                0.55 if mode == "at_time" else 0.6,
                (
                    f"{fact_note}; последним взял дороже — есть более дешёвая "
                    f"допустимая бьющая {cheaper[0]} "
                    f"({card_points(cheaper[0], trump)} < {card_points(card, trump)}); "
                    f"не доказывает, что дешевле всегда лучше"
                ),
                cheaper,
            )
        elif won is True and not cheaper:
            label, category, confidence = "удачный", "подтверждённый_факт", 0.45
            explanation = (
                f"{fact_note}; последним забрал; среди допустимых нет более "
                f"дешёвой гарантированной бьющей"
            )
        elif won is False:
            other_beaters = [c for c in beaters if c != card]
            if other_beaters:
                label, category, confidence, explanation, alternatives = _downgrade_untrusted(
                    "возможная_ошибка",
                    "возможная_ошибка",
                    0.55,
                    (
                        f"{fact_note}; последним отдал, хотя среди допустимых была "
                        f"карта, забирающая текущий стол ({other_beaters[0]}); "
                        f"не доказывает, что взятку обязательно нужно было брать"
                    ),
                    other_beaters,
                )
            else:
                label, category, confidence = "спорный", "спорный_ход", 0.35
                explanation = (
                    f"{fact_note}; последним отдал; среди допустимых нет "
                    f"забирающей карты"
                )
        else:
            label, category, confidence = "спорный", "спорный_ход", 0.2
            explanation = f"{fact_note}; результат взятки неизвестен"

    # при неизвестном козыре не оставляем «подтверждённую» ошибку
    if trump_unknown and category in ("возможная_ошибка", "подтверждённый_факт", "соответствует_fallen"):
        label, category = "кандидат_на_разбор", "кандидат_на_разбор"
        confidence = min(confidence, 0.25)
        explanation = (explanation or "") + "; козырь неизвестен — оценка не подтверждена"
        trusted = False

    out = {
        "mode": mode,
        "label": label,
        "category": category,
        "confidence": confidence,
        "explanation": explanation,
        "method": ANALYZER_VERSION,
        "fact": fact,
        "alternatives": alternatives,
        "limits": limits,
        "legalUsed": legal,
        "legalTrusted": trusted,
        "trumpKnown": not trump_unknown,
        "trumpOpenKnown": trump_open is not None,
        "noTrumpMode": (not trump_unknown) and trump is None,
    }
    if strategy_note:
        out["strategyMatch"] = strategy_note
    if is_fallen_rule and fallen_rule_id:
        out["fallenRule"] = {
            "ruleId": fallen_rule_id,
            "note": "соответствует правилу Fallen — не доказательство лучшего хода",
        }
    elif strategy_note and strategy_note.get("ruleId"):
        out["fallenRule"] = None
    return out


def _index_decisions(events: list[dict]) -> dict:
    """Индекс решений агента: (handId, seat, action) → лучшее server_confirmed."""
    by_key: dict[tuple, dict] = {}
    by_id: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") != "decision":
            continue
        did = str(ev.get("decisionId") or ev.get("decision_id") or "")
        if did:
            prev = by_id.get(did)
            # предпочитаем server_confirmed
            if prev is None or (ev.get("serverConfirmed") and not prev.get("serverConfirmed")):
                by_id[did] = ev
        action = ev.get("confirmedAction")
        if action is None:
            action = ev.get("chosenAction") or ev.get("intentAction")
        if action is None:
            continue
        hid = str(ev.get("handId") if ev.get("handId") is not None else ev.get("hand_id") or "")
        seat = ev.get("seat")
        if seat is None:
            continue
        key = (hid, int(seat), int(action))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = ev
        elif ev.get("serverConfirmed") and not prev.get("serverConfirmed"):
            by_key[key] = ev
        elif ev.get("confirmStatus") == "server_confirmed" and prev.get("confirmStatus") != "server_confirmed":
            by_key[key] = ev
    return {"by_key": by_key, "by_id": by_id}


def analyze_game(events: list[dict], reconstructions: list[dict] | None = None) -> list[dict]:
    """Решения всех игроков по событиям игры + реконструкциям."""
    from .reconstruct import reconstruct_game

    recon = reconstructions if reconstructions is not None else reconstruct_game(events)
    recon_by_hand = {str(r.get("handId")): r for r in recon}
    meta = next((e for e in events if e.get("type") == "meta"), {})
    seats = int(meta.get("seats") or meta.get("numPlayers") or meta.get("players") or 3)
    our_seat = meta.get("seat")
    try:
        our_seat = int(our_seat) if our_seat is not None else None
    except (TypeError, ValueError):
        our_seat = None

    trump_by_hand: dict[str, int | None] = {}
    declarer_by_hand: dict[str, int | None] = {}
    trump_open_by_hand: dict[str, bool] = {}
    for e in events:
        if e.get("type") in ("hand_start", "trump") and e.get("handId") is not None:
            hid = str(e["handId"])
            if e.get("trump") is not None:
                trump_by_hand[hid] = e.get("trump")
            if e.get("declarer") is not None:
                declarer_by_hand[hid] = e.get("declarer")
            if e.get("trumpOpen") is not None:
                trump_open_by_hand[hid] = bool(e.get("trumpOpen"))

    dec_index = _index_decisions(events)
    linked_decision_ids: set[str] = set()

    decisions = []
    current_hand = None
    trick_plays: list[dict] = []

    def flush_trick(winner, points, hand_id, trick_no):
        nonlocal trick_plays
        hid_key = str(hand_id)
        trump_unknown = hid_key not in trump_by_hand
        trump = trump_by_hand.get(hid_key)  # None + known → NT; absent key → unknown
        declarer = declarer_by_hand.get(hid_key)
        # неизвестный trumpOpen ≠ True
        trump_open = trump_open_by_hand.get(hid_key)  # may be None
        recon_h = recon_by_hand.get(hid_key) or {}
        seats_info = recon_h.get("seats") or {}
        n_plays = len(trick_plays)
        seat_ids = [int(p["seat"]) for p in trick_plays if p.get("seat") is not None]
        # полный состав + уникальные места
        trick_complete = (
            n_plays == int(seats)
            and n_plays > 0
            and len(set(seat_ids)) == n_plays
        )

        for i, play in enumerate(trick_plays):
            seat = int(play["seat"])
            card = int(play["card"])
            table_before = [(int(p["seat"]), int(p["card"])) for p in trick_plays[:i]]
            seat_rec = seats_info.get(str(seat)) or {}
            hand_before = None
            hand_partial = False
            steps = seat_rec.get("handBeforePlays") or []
            for step in steps:
                if step.get("cardPlayed") == card and (
                    step.get("trickNo") == trick_no or trick_no is None
                ):
                    hb = step.get("handBefore")
                    if isinstance(hb, list):
                        hand_before = list(hb)
                    break
            # старые журналы: рассинхрон trickNo — берём шаг по cardPlayed, если однозначен
            if hand_before is None:
                matches = [
                    step for step in steps
                    if step.get("cardPlayed") == card and isinstance(step.get("handBefore"), list)
                ]
                if len(matches) == 1:
                    hand_before = list(matches[0]["handBefore"])
            # НЕ подставляем initialHand целиком вместо остатка
            if hand_before is None:
                hand_partial = True

            won = winner is not None and int(winner) == seat
            is_last = bool(trick_complete and i == n_plays - 1)
            if not trick_complete:
                # не объявляем последнего при неполной взятке
                is_last = False

            us = bool(play.get("us"))
            if our_seat is not None and seat == our_seat:
                us = True

            linked = None
            if us:
                linked = dec_index["by_key"].get((str(hand_id), seat, card))
            legal = None
            fallen = play.get("ruleId")
            reason = None
            decision_id = f"{hand_id}-{trick_no}-{seat}-{card}"
            if linked is not None:
                legal = linked.get("legalActions") or linked.get("legal_actions")
                fallen = linked.get("ruleId") or linked.get("rule_id") or fallen
                reason = linked.get("reason")
                did = linked.get("decisionId") or linked.get("decision_id")
                if did:
                    decision_id = str(did)
                    linked_decision_ids.add(str(did))

            at_hand = hand_before if us else None
            at_time = evaluate_play(
                seat=seat,
                card=card,
                hand_before=at_hand,
                table_before=table_before,
                trump=trump,
                won=won,
                trick_points=points if won else 0,
                legal_actions=legal if us else None,
                fallen_rule_id=fallen,
                mode="at_time",
                is_last=is_last,
                seats=seats,
                declarer=declarer,
                trump_open=trump_open,
                trump_unknown=trump_unknown,
            )
            if not us:
                at_time["explanation"] = (
                    "на момент хода рука соперника скрыта; "
                    + (at_time.get("explanation") or "")
                )
                at_time["category"] = "недостаточно_данных"
                if at_time.get("label") not in ("неоценённый",):
                    at_time["label"] = "спорный"
                    at_time["confidence"] = min(float(at_time.get("confidence") or 0), 0.2)

            post = evaluate_play(
                seat=seat,
                card=card,
                hand_before=hand_before,
                table_before=table_before,
                trump=trump,
                won=won,
                trick_points=points if won else 0,
                legal_actions=legal if us else None,
                fallen_rule_id=fallen,
                mode="post_reveal",
                is_last=is_last,
                seats=seats,
                declarer=declarer,
                trump_open=trump_open,
                trump_unknown=trump_unknown,
            )
            if hand_partial:
                post["limits"] = list(post.get("limits") or []) + [
                    "рука перед ходом не восстановлена последовательно"
                ]
                post["category"] = "недостаточно_данных"
                if post.get("category") == "возможная_ошибка" or post.get("label") == "возможная_ошибка":
                    post["label"] = "кандидат_на_разбор"
                    post["category"] = "кандидат_на_разбор"
            if not trick_complete:
                post["limits"] = list(post.get("limits") or []) + [
                    f"взятка неполная: {n_plays}/{seats} ходов"
                ]
            status = seat_rec.get("status")
            if status != "complete":
                post["limits"] = list(post.get("limits") or []) + [
                    f"реконструкция: {status}"
                ]
                post["explanation"] = (
                    f"реконструкция руки: {status}; "
                    + "; ".join(seat_rec.get("reasons") or [])
                    + ("; " + (post.get("explanation") or ""))
                )
                post["confidence"] = min(float(post.get("confidence") or 0), 0.35)
                if status == "invalid":
                    post["label"] = "неоценённый"
                    post["category"] = "недостаточно_данных"

            decisions.append({
                "decisionId": decision_id,
                "handId": hand_id,
                "trickNo": trick_no,
                "seat": seat,
                "us": us,
                "kind": "card_play",
                "card": card,
                "cardToken": play.get("cardToken"),
                "tableBefore": [{"seat": s, "card": c} for s, c in table_before],
                "winner": winner,
                "isLast": is_last,
                "trickComplete": trick_complete,
                "linkedDecision": bool(linked),
                "ruleId": fallen,
                "reason": reason,
                "legalActions": legal,
                "evalAtTime": at_time,
                "evalPostReveal": post,
                "reconstructionStatus": status,
                "handBeforeKnown": hand_before is not None,
            })
        trick_plays = []

    for ev in events:
        et = ev.get("type")
        if et == "hand_start":
            current_hand = ev.get("handId")
            trick_plays = []
            if ev.get("trump") is not None:
                trump_by_hand[str(current_hand)] = ev.get("trump")
            if ev.get("declarer") is not None:
                declarer_by_hand[str(current_hand)] = ev.get("declarer")
        elif et == "trump" and ev.get("handId") is not None:
            trump_by_hand[str(ev["handId"])] = ev.get("trump")
            if ev.get("declarer") is not None:
                declarer_by_hand[str(ev["handId"])] = ev.get("declarer")
            if ev.get("trumpOpen") is not None:
                trump_open_by_hand[str(ev["handId"])] = bool(ev.get("trumpOpen"))
        elif et == "play":
            trick_plays.append(ev)
        elif et == "trick_end":
            flush_trick(
                ev.get("winner"),
                ev.get("points"),
                ev.get("handId", current_hand),
                ev.get("trickNo"),
            )

    # торг / некарточные — только server_confirmed и не слинкованные с play
    for ev in events:
        if ev.get("type") != "decision":
            continue
        did = str(ev.get("decisionId") or ev.get("decision_id") or "")
        if did and did in linked_decision_ids:
            continue  # уже учтён как card_play
        if not (ev.get("serverConfirmed") or ev.get("confirmStatus") == "server_confirmed"):
            continue
        action = ev.get("confirmedAction")
        if action is None:
            action = ev.get("chosenAction")
        # карточный action 0..31 без слинкованного play — не дублируем как отдельный ход
        if isinstance(action, int) and 0 <= int(action) <= 31:
            continue
        decisions.append({
            "decisionId": did or None,
            "handId": ev.get("handId") or ev.get("hand_id"),
            "seat": ev.get("seat"),
            "us": True,
            "kind": "agent_decision",
            "chosenAction": action,
            "legalActions": ev.get("legalActions") or ev.get("legal_actions"),
            "ruleId": ev.get("ruleId") or ev.get("rule_id"),
            "reason": ev.get("reason"),
            "evalAtTime": {
                "mode": "at_time",
                "label": "неоценённый",
                "category": "недостаточно_данных",
                "confidence": 0.0,
                "explanation": "торг/некарточный ход — правила взятки не применяются",
                "method": ANALYZER_VERSION,
                "fallenRule": {
                    "ruleId": ev.get("ruleId") or ev.get("rule_id"),
                    "note": "соответствует правилу Fallen — не доказательство лучшего хода",
                } if (ev.get("ruleId") or ev.get("rule_id")) else None,
            },
            "evalPostReveal": {
                "mode": "post_reveal",
                "label": "неоценённый",
                "category": "недостаточно_данных",
                "confidence": 0.0,
                "explanation": "торг/некарточный ход",
                "method": ANALYZER_VERSION,
            },
        })

    return decisions
