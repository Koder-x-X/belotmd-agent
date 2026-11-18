"""Агент strong2: усиленная эвристика поверх strong.

Отличия от strong:
- агрессивнее/точнее торги (тузы, валеты, девятки, отставание в счёте);
- ход с учётом void'ов и «хозяев» масти;
- эндшпиль и гонка по очкам;
- втроём без логики партнёра.

Модель не использует — чистая эвристика для сравнения со strong.
Всегда уважает legal_mask.
"""

from __future__ import annotations

import numpy as np

from .base import register
from .strong_agent import (
    _T_POWER,
    _NT_POWER,
    _answer_high_or_queen,
    _beats,
    _cheapest_winner,
    _current_winner_card,
    _dump_under_enemy_trump,
    _known_voids,
    _keep_priority,
    _lead_penalty,
    _pick_safest,
    _play_power,
    _seen_cards,
    _strength,
    _suit_ace_out,
    _unsafe_ten,
    card_points,
    trick_winner_seat,
)


def choose_strong2(state, seat, match_scores, legal_mask) -> int:
    legal = [int(a) for a in np.flatnonzero(legal_mask)]
    if not legal:
        raise ValueError("act() called with an empty legal mask")
    hand = list(state.hands[seat]) if 0 <= seat < len(state.hands) else []
    if getattr(state, "phase", "") == "BIDDING":
        return _bid2(state, seat, hand, legal, match_scores)
    return _play2(state, seat, hand, legal, match_scores)


def _score_pressure(seat, match_scores, seats: int) -> float:
    """>0 если мы отстаём, <0 если впереди. Норма около ±1."""
    scores = list(match_scores or [])
    if not scores:
        return 0.0
    if seats == 3:
        ours = scores[seat] if seat < len(scores) else 0
        best_other = max((scores[i] for i in range(len(scores)) if i != seat), default=0)
        return (best_other - ours) / 50.0
    if len(scores) >= 2:
        ours = scores[seat % 2]
        theirs = scores[1 - (seat % 2)]
        return (theirs - ours) / 50.0
    return 0.0


def _suit_power(hand, suit, extra=None) -> tuple:
    length, points = _strength(hand, suit, extra=extra)
    jacks = sum(1 for c in hand if c // 8 == suit and c % 8 == 4)
    nines = sum(1 for c in hand if c // 8 == suit and c % 8 == 2)
    aces = sum(1 for c in hand if c // 8 == suit and c % 8 == 7)
    if extra is not None and extra // 8 == suit:
        r = extra % 8
        jacks += int(r == 4)
        nines += int(r == 2)
        aces += int(r == 7)
    score = points + 22 * jacks + 12 * nines + 8 * aces + 3 * max(0, length - 2)
    return length, points, jacks, nines, aces, score


def _bid2(state, seat, hand, legal, match_scores):
    seats = int(getattr(state, "num_players", 4) or 4)
    pressure = _score_pressure(seat, match_scores, seats)
    # Принять лицо
    if 33 in legal:
        length, points, jacks, nines, aces, score = _suit_power(
            hand, state.face_up_suit, extra=state.face_up_card
        )
        need = 34 if pressure > 0.4 else 38 if pressure < -0.3 else 36
        strong = (
            score >= need
            or length >= 4
            or (length >= 3 and jacks)
            or (length >= 3 and points >= 20)
            or (length >= 3 and jacks + nines + aces >= 2)
        )
        if strong:
            return 33
        return 32 if 32 in legal else legal[0]

    suits = [a - 34 for a in legal if 34 <= a <= 37]
    if suits:
        ranked = sorted(suits, key=lambda s: _suit_power(hand, s)[5], reverse=True)
        best = ranked[0]
        length, points, jacks, nines, aces, score = _suit_power(hand, best)
        # Порог объявления: ниже при отставании
        bar = 40 if pressure > 0.5 else 48 if pressure < -0.4 else 44
        if 32 in legal and score < bar and not (length >= 4 and jacks):
            return 32
        return 34 + best
    return 32 if 32 in legal else legal[0]


def _trick_points(trick, trump):
    return sum(card_points(c, trump) for _, c in trick)


def _is_master(card, trump, grave, hand_others_gone):
    """Грубо: нет ли на руках у кого-то выше этой карты в масти/козыре."""
    suit = card // 8
    rank = card % 8
    if trump is not None and suit == trump:
        for r, p in _T_POWER.items():
            if p <= _T_POWER[rank]:
                continue
            higher = suit * 8 + r
            if higher not in grave and higher not in hand_others_gone:
                return False
        return True
    for r, p in _NT_POWER.items():
        if p <= _NT_POWER[rank]:
            continue
        higher = suit * 8 + r
        if higher not in grave and higher not in hand_others_gone:
            return False
    return True


def _play2(state, seat, hand, legal, match_scores):
    cards = [a for a in legal if 0 <= a <= 31]
    if not cards:
        return legal[0]

    trump = state.trump
    trick = list(state.current_trick or [])
    seats = int(getattr(state, "num_players", 4) or 4)
    team_play = seats == 4
    pressure = _score_pressure(seat, match_scores, seats)
    endgame = len(hand) <= 3
    seen = _seen_cards(state, seat, hand)

    if not trick:
        return _lead2(cards, hand, trump, state, seat, pressure, endgame)

    led_suit = trick[0][1] // 8
    current = _current_winner_card(trick, trump)
    leader = trick_winner_seat(trick, trump)
    partner_winning = team_play and leader is not None and (leader % 2) == (seat % 2)
    remaining = seats - len(trick)
    last = remaining <= 1
    winners = [c for c in cards if _beats(c, current, trump, led_suit)]
    table_pts = _trick_points(trick, trump)
    opp_threat = _opponents_threat(state, seat, trick, trump, led_suit, current)

    # Партнёр держит взятку
    if partner_winning:
        if last or not opp_threat:
            safe = [c for c in cards if not _unsafe_ten(c, trump, seen)] or cards
            return _pick_safest(
                safe, trump, seen,
                prefer_high_points=True,
                match_scores=match_scores,
                winner_seat=leader,
                num_players=seats,
            )
        if winners:
            return _cheapest_winner(winners, trump, led_suit, seen)
        return _pick_safest(
            cards, trump, seen,
            match_scores=match_scores, winner_seat=leader, num_players=seats,
        )

    under = _dump_under_enemy_trump(
        cards, winners, trump, led_suit, current, partner_winning,
    )
    if under is not None:
        return under

    special = _answer_high_or_queen(cards, winners, trump, led_suit, current, seen)
    if special is not None:
        return special

    if winners:
        if last:
            return _cheapest_winner(winners, trump, led_suit, seen)
        ace_threat = led_suit != trump and _suit_ace_out(led_suit, seen) and not last
        if table_pts >= 14 or (endgame and table_pts >= 6) or (pressure > 0.6 and table_pts >= 8):
            pool = winners
            if ace_threat:
                no_ten = [c for c in winners if not _unsafe_ten(c, trump, seen)]
                if no_ten:
                    pool = no_ten
            return max(pool, key=lambda c: (
                _play_power(c, trump, led_suit),
                card_points(c, trump),
            ))
        return _cheapest_winner(winners, trump, led_suit, seen)

    # Сброс: 10 — отстающему по партии, даму — лидеру
    return _pick_safest(
        cards, trump, seen,
        match_scores=match_scores, winner_seat=leader, num_players=seats,
    )


def _opponents_threat(state, seat, trick, trump, led_suit, current) -> bool:
    seats = int(getattr(state, "num_players", 4) or 4)
    played = {s for s, _ in trick}
    remaining_seats = [
        (seat + k) % seats
        for k in range(1, seats)
        if (seat + k) % seats not in played and (seat + k) % seats != seat
    ]
    if seats == 4:
        opponents = [s for s in remaining_seats if (s % 2) != (seat % 2)]
    else:
        opponents = list(remaining_seats)
    if not opponents:
        return False
    if current is None:
        return True
    voids = _known_voids(state, seat)
    for opp in opponents:
        vs = voids.get(opp, set())
        can_follow = led_suit not in vs
        can_trump = trump is not None and trump not in vs
        if can_follow or can_trump:
            return True
    return False


def _lead2(cards, hand, trump, state, seat, pressure, endgame):
    voids = _known_voids(state, seat)
    seats = int(getattr(state, "num_players", 4) or 4)
    seen = _seen_cards(state, seat, hand)
    our = set(hand)

    def lead_score(card):
        suit = card // 8
        is_trump = trump is not None and suit == trump
        length = sum(1 for c in hand if c // 8 == suit)
        pts = card_points(card, trump)
        # Козырь: J > 9 > A > 10 > K > Q — только _T_POWER, не обычная масть
        power = _T_POWER[card % 8] if is_trump else _NT_POWER[card % 8]
        trump_pen = 5 if is_trump and power < 6 and not endgame else 0
        master_bonus = 0
        if _is_master(card, trump, seen - our, our):
            master_bonus = 5 if not is_trump else 3
        void_pen = 0
        for s, vs in voids.items():
            if seats == 4 and (s % 2) == (seat % 2):
                continue
            if s == seat:
                continue
            if suit in vs and trump is not None and trump not in vs and not is_trump:
                void_pen += 3
        chase = power if pressure > 0.4 else 0
        risk = _lead_penalty(card, trump, seen)
        return (power + master_bonus + chase - trump_pen - void_pen - risk, length, pts)

    return max(cards, key=lead_score)


class Strong2Agent:
    name = "strong2"

    def __init__(self, **_ignored):
        self.strategy = "heuristic2"

    def reset(self):
        return None

    def snapshot(self):
        return None

    def restore(self, snapshot):
        return None

    def describe(self) -> str:
        return f"strong2/{self.strategy}"

    def act(self, state, seat, match_scores, legal_mask):
        legal = np.asarray(legal_mask, dtype=np.int8)
        if not legal.any():
            raise ValueError("act() called with an empty legal mask")
        return choose_strong2(state, seat, match_scores, legal)


@register("strong2")
def _build(**kwargs):
    return Strong2Agent(**kwargs)
