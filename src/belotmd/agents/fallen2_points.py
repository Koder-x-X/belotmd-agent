"""Оценка ожидаемых очков продолжения раздачи для Fallen2 v3.

Не использует скрытые реальные руки: только наблюдения + сэмплы
детерминизаций. Playout чужих/наших последующих ходов — strong (не Fallen2),
чтобы не подтверждать себя круговым образом.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from belotmd.game.belief import Infeasible, constraints, uniform_sampler
from belotmd.game.state import BelotState

from .strong_agent import (
    _beats,
    _current_winner_card,
    card_points,
    trick_winner_seat,
)

LAST_TRICK_BONUS = 10


@dataclass
class MoveEval:
    card: int
    expected: float
    std: float
    n_worlds: int
    min_v: float
    max_v: float


@dataclass
class SearchReport:
    baseline: int
    chosen: int
    margin: float
    overridden: bool
    reason: str
    evals: list[MoveEval] = field(default_factory=list)
    worlds_used: int = 0
    elapsed_ms: float = 0.0
    fallback: str | None = None
    approx_note: str = (
        "оценка приблизительная: сэмплы рук + playout strong; "
        "не гарантия исхода"
    )


def our_side(seat: int, winner: int, n: int) -> bool:
    if n == 4:
        return (winner % 2) == (seat % 2)
    return winner == seat


def trick_points(trick: list[tuple[int, int]], trump) -> int:
    return sum(card_points(c, trump) for _, c in trick)


def _clone_state_for_world(state, hands: list[list[int]]) -> BelotState:
    """Копия позиции с полной раздачей рук (для симуляции)."""
    st = BelotState()
    st.num_players = int(getattr(state, "num_players", 4) or 4)
    st.phase = "PLAYING"
    st.trump = getattr(state, "trump", None)
    st.declarer = getattr(state, "declarer", None)
    st.dealer = getattr(state, "dealer", 0)
    st.declarer_has_played_trump = bool(
        getattr(state, "declarer_has_played_trump", False)
    )
    st.declaring_team = getattr(state, "declaring_team", None)
    st.defending_team = getattr(state, "defending_team", None)
    st.current_player = int(getattr(state, "current_player", 0) or 0)
    st.current_trick = list(getattr(state, "current_trick", None) or [])
    st.last_trick = list(getattr(state, "last_trick", None) or [])
    st.graveyard = list(getattr(state, "graveyard", None) or [])
    st.tricks_played = int(getattr(state, "tricks_played", 0) or 0)
    st.raw_points_by_team = [0, 0]
    st.tricks_won_by_team = [0, 0]
    st.done = False
    st.hands = [list(h) for h in hands]
    while len(st.hands) < 4:
        st.hands.append([])
    st.impossible_cards = np.zeros((4, 32), dtype=bool)
    st.known_cards = np.zeros((4, 32), dtype=bool)
    for s, h in enumerate(st.hands):
        for c in h:
            if 0 <= int(c) < 32:
                st.known_cards[s, int(c)] = True
    st.combinations = list(getattr(state, "combinations", ["", "", "", ""]) or ["", "", "", ""])
    st.beliefs_degraded = False
    return st


def _apply_card(st: BelotState, seat: int, card: int) -> int | None:
    """Сыграть карту; если взятка закрылась — начислить очки стороне и вернуть winner."""
    st.hands[seat] = [c for c in st.hands[seat] if int(c) != int(card)]
    st.current_trick.append((seat, int(card)))
    if st.trump is not None and int(card) // 8 == st.trump:
        st.declarer_has_played_trump = True
    n = int(st.num_players)
    if len(st.current_trick) < n:
        st.current_player = (seat + 1) % n
        return None
    winner = trick_winner_seat(st.current_trick, st.trump)
    pts = trick_points(st.current_trick, st.trump)
    # последняя взятка раздачи
    remaining = sum(len(h) for h in st.hands[:n])
    if remaining == 0:
        pts += LAST_TRICK_BONUS
    if not hasattr(st, "_pts_by_seat"):
        st._pts_by_seat = [0, 0, 0, 0]  # type: ignore[attr-defined]
    if n == 4:
        st.raw_points_by_team[winner % 2] += pts
        st.tricks_won_by_team[winner % 2] += 1
        st._pts_by_seat[winner] += pts  # type: ignore[attr-defined]
    else:
        st._pts_by_seat[int(winner)] += pts  # type: ignore[attr-defined]
    st.graveyard.extend(c for _, c in st.current_trick)
    st.last_trick = list(st.current_trick)
    st.current_trick = []
    st.tricks_played += 1
    st.current_player = int(winner)
    if remaining == 0:
        st.done = True
    return int(winner)


def _side_points(st: BelotState, seat: int) -> float:
    n = int(st.num_players)
    if n == 4:
        return float(st.raw_points_by_team[seat % 2])
    pts = getattr(st, "_pts_by_seat", None)
    if pts is None:
        return 0.0
    return float(pts[seat])


def _greedy_card(st: BelotState, seat: int, policy: Callable) -> int:
    legal = st.get_legal_actions()
    if not legal.any():
        hand = st.hands[seat]
        return int(hand[0]) if hand else 0
    try:
        return int(policy(st, seat, [0] * st.num_players, legal))
    except Exception:
        cards = [int(a) for a in np.flatnonzero(legal) if 0 <= int(a) <= 31]
        return cards[0] if cards else 0


def playout_from(
    st: BelotState,
    seat: int,
    first_card: int,
    policy: Callable,
    deadline: float,
) -> float:
    """Сыграть first_card от seat, затем до конца жадной политикой. → очки нашей стороны."""
    before = _side_points(st, seat)
    if st.current_player != seat:
        st.current_player = seat
    _apply_card(st, seat, first_card)
    guard = 0
    while not st.done and guard < 64:
        guard += 1
        if time.monotonic() > deadline:
            break
        n = int(st.num_players)
        if all(len(st.hands[s]) == 0 for s in range(n)) and not st.current_trick:
            st.done = True
            break
        actor = int(st.current_player)
        if not st.hands[actor] and not st.current_trick:
            st.done = True
            break
        if not st.hands[actor]:
            # пропускать нельзя — аварийный выход
            break
        card = _greedy_card(st, actor, policy)
        if card not in st.hands[actor]:
            card = int(st.hands[actor][0])
        _apply_card(st, actor, card)
    return _side_points(st, seat) - before


def sample_worlds(state, seat: int, n: int, rng: np.random.Generator) -> list[list[list[int]]]:
    """Список раздач hands[seat]->cards. Пустой список при невозможности.

    3p: учитывает undealt kitty (stock) через StockAwareSampler — иначе
    pool>need и каждый ход был EV-Fallback no_determinization.
    """
    out: list[list[list[int]]] = []
    try:
        sampler = uniform_sampler(state, seat)
    except Infeasible:
        return out
    except Exception:
        return out
    n_seats = int(getattr(state, "num_players", 4) or 4)
    if n_seats not in (2, 3, 4):
        n_seats = 4
    for _ in range(max(1, n)):
        try:
            hands = sampler.draw(rng)
            hands = [list(hands[i]) if i < len(hands) else [] for i in range(4)]
            for i in range(n_seats, 4):
                hands[i] = []
            out.append(hands)
        except Exception:
            break
    return out


def _heuristic_evals(state, seat: int, legal_cards: list[int]) -> list[MoveEval]:
    """Грубый ранг без сэмпла — лучше, чем пустой EV-Fallback."""
    evals: list[MoveEval] = []
    for card in legal_cards:
        v = float(heuristic_immediate(state, seat, card))
        evals.append(MoveEval(card, v, 0.0, 1, v, v))
    return evals


def evaluate_legal_moves(
    state,
    seat: int,
    legal_cards: list[int],
    *,
    policy: Callable,
    n_worlds: int,
    time_budget_ms: float,
    rng: np.random.Generator | None = None,
) -> tuple[list[MoveEval], int, str | None]:
    """Оценить каждую legal карту. Возвращает (evals, worlds_used, fallback).

    fallback is None when real determinizations were used. If sampling fails,
    returns heuristic evals with fallback=\"heuristic\" (still usable for
    pick_with_margin) instead of empty \"no_determinization\".
    """
    rng = rng or np.random.default_rng()
    deadline = time.monotonic() + max(0.02, time_budget_ms / 1000.0)
    worlds = sample_worlds(state, seat, n_worlds, rng)
    if not worlds:
        return _heuristic_evals(state, seat, legal_cards), 0, "heuristic"

    scores: dict[int, list[float]] = {c: [] for c in legal_cards}
    used = 0
    for hands in worlds:
        if time.monotonic() > deadline:
            break
        used += 1
        for card in legal_cards:
            if time.monotonic() > deadline:
                break
            if card not in hands[seat]:
                continue
            st = _clone_state_for_world(state, hands)
            # рука должна содержать card
            if card not in st.hands[seat]:
                continue
            val = playout_from(st, seat, card, policy, deadline)
            scores[card].append(val)

    evals: list[MoveEval] = []
    for card in legal_cards:
        xs = scores.get(card) or []
        if not xs:
            evals.append(MoveEval(card, float("-inf"), 0.0, 0, 0.0, 0.0))
            continue
        arr = np.asarray(xs, dtype=float)
        evals.append(
            MoveEval(
                card=card,
                expected=float(arr.mean()),
                std=float(arr.std(ddof=0)),
                n_worlds=len(xs),
                min_v=float(arr.min()),
                max_v=float(arr.max()),
            )
        )
    return evals, used, None


def pick_with_margin(
    evals: list[MoveEval],
    baseline: int,
    margin: float,
) -> tuple[int, bool, str]:
    by = {e.card: e for e in evals if e.n_worlds > 0}
    if baseline not in by or not by:
        return baseline, False, "нет оценки — оставляем Fallen"
    base_ev = by[baseline].expected
    best = max(by.values(), key=lambda e: (e.expected, -e.std, -e.card))
    if best.card == baseline:
        return baseline, False, (
            f"Fallen совпадает с максимумом EV≈{base_ev:.1f}"
        )
    gain = best.expected - base_ev
    if gain >= margin:
        return best.card, True, (
            f"EV {best.expected:.1f} лучше Fallen {base_ev:.1f} "
            f"на {gain:.1f} (≥ порог {margin})"
        )
    return baseline, False, (
        f"оставляем Fallen: выигрыш EV {gain:.1f} < порог {margin}"
    )


def heuristic_immediate(
    state,
    seat: int,
    card: int,
) -> float:
    """Грубая оценка без сэмпла: очки взятки × P(win) − цена карты в руке.

    Когда результат взятки уже ясен (последний ход), очки стола одинаковы для
    всех бьющих — предпочитаем более дешёвую карту (сохраняем силу).
    """
    trump = getattr(state, "trump", None)
    trick = list(getattr(state, "current_trick", None) or [])
    n = int(getattr(state, "num_players", 4) or 4)
    table = trick + [(seat, card)]
    pts = trick_points(table, trump)
    remaining = n - len(table)
    current = _current_winner_card(trick, trump)
    led = trick[0][1] // 8 if trick else card // 8
    wins_now = True if not trick else _beats(card, current, trump, led)
    cost = float(card_points(card, trump))
    hand_left = sum(len(h) for h in (getattr(state, "hands", None) or [])[:n])
    last_bonus = LAST_TRICK_BONUS if hand_left <= 1 else 0
    if remaining <= 0:
        win = our_side(seat, trick_winner_seat(table, trump), n)
        if win:
            # Last-to-trick: points identical for all beaters → strongly
            # prefer the cheapest (cuts the main overpay failure mode).
            return float(pts + last_bonus) - 1.25 * cost
        # проигрыш: сброс дешёвой лучше, чем дарить туза
        return -cost
    # неизвестные ответы: дорогой стол и шанс взять — важнее; иначе бережём силу
    p = 0.55 if wins_now else 0.25
    if not wins_now:
        return -0.6 * cost
    return p * float(pts) - 0.35 * cost
