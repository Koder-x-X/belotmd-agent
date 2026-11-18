"""Восстановление рук после раздачи по наблюдаемым ходам.

Не придумывает недостающие карты. Не использует скрытую информацию,
которой не было в журнале.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any  # noqa: F401 — used in seats_in_trick typing

RECONSTRUCT_VERSION = "2.2"


def _card_id(row: dict) -> int | None:
    if row.get("card") is not None:
        try:
            return int(row["card"])
        except (TypeError, ValueError):
            return None
    return None


def reconstruct_hand(events: list[dict], *, hand_id, seats: int, our_seat: int | None = None) -> dict:
    """Восстановить руки одной раздачи из событий JSONL (уже отфильтрованных или полных)."""
    hand_key = str(hand_id)
    plays: list[dict] = []
    transfers: list[dict] = []
    our_start: list[int] | None = None
    trick_nos = set()

    for ev in events:
        if str(ev.get("handId")) != hand_key and ev.get("handId") != hand_id:
            # meta/final без handId пропускаем; события других раздач — нет
            if ev.get("type") in ("play", "trick_end", "hand_start", "swap", "draw", "exchange"):
                if ev.get("handId") is not None:
                    continue
        et = ev.get("type")
        if et == "hand_start" and (str(ev.get("handId")) == hand_key or ev.get("handId") == hand_id):
            raw = ev.get("ourHand") or ev.get("our_hand")
            if isinstance(raw, list):
                our_start = [int(c) for c in raw]
        elif et == "play" and (str(ev.get("handId")) == hand_key or ev.get("handId") == hand_id):
            plays.append(ev)
            if ev.get("trickNo") is not None:
                trick_nos.add(int(ev["trickNo"]))
        elif et in ("swap", "draw", "exchange") and (
            str(ev.get("handId")) == hand_key or ev.get("handId") == hand_id
        ):
            transfers.append(ev)
        elif et == "trick_end" and (str(ev.get("handId")) == hand_key or ev.get("handId") == hand_id):
            if ev.get("trickNo") is not None:
                trick_nos.add(int(ev["trickNo"]))

    # Только события этой раздачи
    plays = [
        p for p in plays
        if str(p.get("handId")) == hand_key or p.get("handId") == hand_id
    ]

    by_seat: dict[int, list[int]] = defaultdict(list)
    play_order: list[dict] = []
    seen_cards: set[int] = set()
    contradictions: list[str] = []
    seats_in_trick: dict[Any, list[int]] = defaultdict(list)

    for p in plays:
        seat = p.get("seat")
        card = _card_id(p)
        if seat is None or card is None:
            contradictions.append("ход без seat/card")
            continue
        seat = int(seat)
        if card in seen_cards:
            contradictions.append(f"повтор карты {card}")
        seen_cards.add(card)
        tno = p.get("trickNo")
        if tno is not None:
            prev_seats = seats_in_trick[int(tno)]
            if seat in prev_seats:
                contradictions.append(f"повтор места {seat} во взятке {tno}")
            prev_seats.append(seat)
        by_seat[seat].append(card)
        play_order.append({
            "seat": seat,
            "card": card,
            "trickNo": p.get("trickNo"),
            "t": p.get("t"),
            "us": bool(p.get("us")),
        })

    unknown_transfers = []
    for tr in transfers:
        if tr.get("cardsKnown") is False or tr.get("unknown"):
            unknown_transfers.append(tr)
        elif not tr.get("cards") and not tr.get("card"):
            unknown_transfers.append(tr)

    n_tricks = len(trick_nos) or max((len(v) for v in by_seat.values()), default=0)
    # В белот каждый игрок кладёт ровно одну карту на взятку → ожидаем n_tricks карт с места.
    expected = n_tricks if n_tricks > 0 else None
    # Полная раздача по правилам режима: обычно 8 взяток / 8 карт на место (3p и 4p).
    # Одна взятка с hand_start/hand_end ≠ complete.
    expected_tricks_full = 8 if int(seats) in (3, 4) else None
    has_hand_start = any(
        e.get("type") == "hand_start"
        and (str(e.get("handId")) == hand_key or e.get("handId") == hand_id)
        for e in events
    )
    has_hand_end = any(
        e.get("type") == "hand_end"
        and (str(e.get("handId")) == hand_key or e.get("handId") == hand_id)
        for e in events
    )
    our_expected = len(our_start) if our_start is not None else None

    seats_out = {}
    overall = "complete"
    global_reasons: list[str] = []
    if contradictions:
        overall = "invalid"
    elif unknown_transfers:
        overall = "partial"
        global_reasons.append("неизвестные обмены/доборы")
    elif not has_hand_end:
        overall = "partial"
        global_reasons.append("нет hand_end")
    elif not has_hand_start:
        overall = "partial"
        global_reasons.append("нет hand_start")
    elif expected_tricks_full is not None and n_tricks < expected_tricks_full:
        overall = "partial"
        global_reasons.append(
            f"взяток {n_tricks} < ожидаемых {expected_tricks_full} для полной раздачи"
        )
    elif expected_tricks_full is not None and n_tricks > expected_tricks_full:
        overall = "invalid"
        global_reasons.append(
            f"взяток {n_tricks} > ожидаемых {expected_tricks_full} — не нормальная полная раздача"
        )
    # полнота каждой взятки: ровно seats уникальных мест
    for tno, seat_list in seats_in_trick.items():
        if len(seat_list) != int(seats):
            if overall == "complete":
                overall = "partial"
            global_reasons.append(
                f"взятка {tno}: ходов {len(seat_list)}, ожидалось {seats}"
            )
        if len(set(seat_list)) != len(seat_list):
            overall = "invalid"
            global_reasons.append(f"взятка {tno}: неуникальные места {seat_list}")

    for seat in range(int(seats)):
        cards = list(by_seat.get(seat, []))
        status = "complete"
        reasons: list[str] = []
        if contradictions:
            status = "invalid"
            reasons.extend(contradictions)
        if unknown_transfers:
            status = "partial"
            reasons.append("есть добор/обмен с неизвестным содержимым")
        reasons.extend(global_reasons)
        if not has_hand_end:
            status = "partial" if status == "complete" else status
            if "нет hand_end" not in reasons:
                reasons.append("нет hand_end — раздача не завершена в журнале")
        if not has_hand_start:
            status = "partial" if status == "complete" else status
            if "нет hand_start" not in reasons:
                reasons.append("нет hand_start")
        if expected_tricks_full is not None and n_tricks < expected_tricks_full:
            status = "partial" if status == "complete" else status
        if expected is not None and len(cards) != expected:
            status = "partial" if status == "complete" else status
            reasons.append(f"сыграно {len(cards)} карт, ожидалось {expected} по числу взяток")
        if (
            our_expected is not None
            and our_seat is not None
            and seat == int(our_seat)
            and len(cards) != our_expected
            and has_hand_end
        ):
            status = "partial" if status == "complete" else status
            reasons.append(
                f"сыграно {len(cards)}, в стартовой руке {our_expected}"
            )
        if not cards:
            status = "partial"
            reasons.append("нет сыгранных карт")
        # Начальная рука = множество сыгранных (порядок неизвестен), если нет неизвестных перемещений
        initial = sorted(cards) if status != "invalid" and not unknown_transfers else sorted(cards)
        if our_seat is not None and seat == int(our_seat) and our_start is not None:
            # Наша рука из hand_start надёжнее порядка; сверка с сыгранными
            start_set = sorted(int(c) for c in our_start)
            if set(start_set) != set(cards) and cards:
                # могли остаться несыгранные только если раздача оборвалась
                if not set(cards).issubset(set(start_set)):
                    status = "invalid"
                    reasons.append("сыгранные карты не входят в зафиксированную нашу руку")
                else:
                    initial = start_set
                    if set(start_set) != set(cards):
                        status = "partial" if status == "complete" else status
                        reasons.append("раздача оборвана: не все карты из руки сыграны")
            else:
                initial = start_set or initial

        # Рука перед каждым ходом этого места
        before = []
        remaining = list(initial)
        for step in play_order:
            if step["seat"] != seat:
                continue
            before.append({
                "cardPlayed": step["card"],
                "trickNo": step["trickNo"],
                "handBefore": sorted(remaining),
            })
            if step["card"] in remaining:
                remaining.remove(step["card"])
            else:
                status = "invalid"
                reasons.append(f"ход картой {step['card']} отсутствует в восстановленной руке")

        seats_out[str(seat)] = {
            "seat": seat,
            "status": status,
            "initialHand": initial,
            "played": cards,
            "handBeforePlays": before,
            "remainingAfter": sorted(remaining),
            "reasons": reasons,
        }
        if status == "invalid":
            overall = "invalid"
        elif status == "partial" and overall == "complete":
            overall = "partial"

    return {
        "version": RECONSTRUCT_VERSION,
        "handId": hand_id,
        "status": overall,
        "seats": seats_out,
        "tricks": n_tricks,
        "transfers": [
            {"type": t.get("type"), "unknown": True} for t in unknown_transfers
        ],
        "contradictions": contradictions,
    }


def reconstruct_game(events: list[dict]) -> list[dict]:
    """Все раздачи игры — в т.ч. без hand_start (ТЗ §4)."""
    meta = next((e for e in events if e.get("type") == "meta"), {})
    seats = int(meta.get("seats") or meta.get("numPlayers") or meta.get("players") or 3)
    our_seat = meta.get("seat")
    hand_ids: list = []
    seen = set()

    def _add(hid):
        key = str(hid)
        if key in seen:
            return
        seen.add(key)
        hand_ids.append(hid)

    for e in events:
        if e.get("type") == "hand_start" and e.get("handId") is not None:
            _add(e["handId"])
    # Все раздачи из событий, даже без hand_start
    for e in events:
        if e.get("type") in ("play", "trick_end", "hand_end", "trump", "swap", "draw") and e.get("handId") is not None:
            _add(e["handId"])
    return [
        reconstruct_hand(events, hand_id=hid, seats=seats, our_seat=our_seat)
        for hid in hand_ids
    ]
