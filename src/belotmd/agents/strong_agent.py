"""Агент strong: улучшенная эвристика и опциональная модель.

Без модели работает сразу. С совместимым чекпоинтом оценивает только
разрешённые действия. Всегда уважает legal_mask.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from .base import register

# Общий источник силы/очков карт — как в BelotState.card_value
# Козырь по силе: J(4) > 9(2) > A(7) > 10(3) > K(6) > Q(5) > 8 > 7
# Козырь по очкам: J=20, 9=14, A=11, 10=10, K=4, Q=3
_T_POWER = {0: 0, 1: 1, 5: 2, 6: 3, 3: 4, 7: 5, 2: 6, 4: 7}
_NT_POWER = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4, 6: 5, 3: 6, 7: 7}
_T_PTS = {0: 0, 1: 0, 2: 14, 3: 10, 4: 20, 5: 3, 6: 4, 7: 11}
_NT_PTS = {0: 0, 1: 0, 2: 0, 3: 10, 4: 2, 5: 3, 6: 4, 7: 11}
RANK_9, RANK_10, RANK_J, RANK_Q, RANK_K, RANK_A = 2, 3, 4, 5, 6, 7


def card_power(card: int, trump) -> tuple:
    """(tier, power): trump > led-suit; чужая некозырная масть = 0."""
    suit = card // 8
    rank = card % 8
    if trump is not None and suit == trump:
        return (2, _T_POWER[rank])
    return (1, _NT_POWER[rank])  # caller must zero out off-suit


def card_points(card: int, trump) -> int:
    if trump is not None and card // 8 == trump:
        return _T_PTS[card % 8]
    return _NT_PTS[card % 8]


def trick_winner_seat(trick, trump) -> int | None:
    """Победитель взятки с учётом козыря и масти первого хода.

    Карта другой некозырной масти не побеждает из-за высокого ранга.
    """
    if not trick:
        return None
    led_suit = trick[0][1] // 8

    def key(played):
        _seat, card = played
        suit = card // 8
        if trump is not None and suit == trump:
            return (2, _T_POWER[card % 8])
        if suit == led_suit:
            return (1, _NT_POWER[card % 8])
        return (0, 0)

    return max(trick, key=key)[0]


def _current_winner_card(trick, trump):
    if not trick:
        return None
    led_suit = trick[0][1] // 8
    best = None
    best_key = (-1, -1)
    for _seat, card in trick:
        suit = card // 8
        if trump is not None and suit == trump:
            key = (2, _T_POWER[card % 8])
        elif suit == led_suit:
            key = (1, _NT_POWER[card % 8])
        else:
            key = (0, 0)
        if key > best_key:
            best_key = key
            best = card
    return best


def _beats(card, current, trump, led_suit) -> bool:
    if current is None:
        return True
    suit = card // 8
    cur_suit = current // 8
    if trump is not None and suit == trump:
        if cur_suit == trump:
            return _T_POWER[card % 8] > _T_POWER[current % 8]
        return True
    if suit != led_suit:
        return False
    if cur_suit == trump and trump is not None:
        return False
    if cur_suit != led_suit:
        return True
    return _NT_POWER[card % 8] > _NT_POWER[current % 8]


def _strength(hand, suit, extra=None):
    cards = [c for c in hand if c // 8 == suit]
    if extra is not None and extra // 8 == suit:
        cards.append(extra)
    return len(cards), sum(_T_PTS[c % 8] for c in cards)


def _known_voids(state, seat):
    """Масти, в которых соперник уже показал void (impossible на все карты масти)."""
    voids = {}
    seats = int(getattr(state, "num_players", 4) or 4)
    for s in range(seats):
        if s == seat:
            continue
        void_suits = set()
        for suit in range(4):
            ids = list(range(suit * 8, suit * 8 + 8))
            if all(bool(state.impossible_cards[s, c]) for c in ids):
                void_suits.add(suit)
        voids[s] = void_suits
    return voids


def _seen_cards(state, seat, hand=None):
    """Карты, которые уже не «гуляют»: сброс, текущая взятка, наша рука."""
    seen = set(state.graveyard or [])
    for _, card in (state.current_trick or []):
        seen.add(card)
    mine = hand
    if mine is None and 0 <= seat < len(getattr(state, "hands", []) or []):
        mine = state.hands[seat]
    if mine:
        seen.update(mine)
    return seen


def _suit_ace_out(suit, seen) -> bool:
    return (suit * 8 + RANK_A) not in seen


def _keep_priority(card, trump) -> int:
    """Насколько карту жалко сбрасывать. В козыре 9/валет дороже дамы и короля."""
    suit, rank = card // 8, card % 8
    if trump is not None and suit == trump:
        return 100 + _T_POWER[rank] * 10 + _T_PTS[rank]
    if rank == RANK_A:
        return 80
    if rank == RANK_10:
        return 70
    if rank == RANK_K:
        return 40
    if rank == RANK_Q:
        return 30
    return _NT_PTS[rank]


def _unsafe_ten(card, trump, seen) -> bool:
    """10 некозырной масти, пока туз этой масти ещё гуляет."""
    suit, rank = card // 8, card % 8
    if rank != RANK_10:
        return False
    if trump is not None and suit == trump:
        return False
    return _suit_ace_out(suit, seen)


def _lead_penalty(card, trump, seen) -> int:
    """Штраф за опасный выход в масть."""
    suit, rank = card // 8, card % 8
    is_trump = trump is not None and suit == trump
    pen = 0
    if _unsafe_ten(card, trump, seen):
        pen += 50  # не ходить с 10, пока туз масти не вышел
    if not is_trump and rank in (RANK_Q, RANK_K) and _suit_ace_out(suit, seen):
        # под гуляющий туз король/дама тоже рискованны, но слабее 10
        pen += 8
    if is_trump and rank in (RANK_Q, RANK_K):
        pen += 6  # ранний мелкий козырь
    if is_trump and rank == RANK_10 and (suit * 8 + RANK_J) not in seen and (suit * 8 + RANK_9) not in seen:
        pen += 4
    return pen


def _match_score_list(match_scores, num_players):
    scores = [int(x) for x in (match_scores or [])]
    if not scores:
        return []
    n = max(1, int(num_players or len(scores)))
    if len(scores) < n:
        scores = scores + [0] * (n - len(scores))
    return scores[:n]


def _trick_winner_is_trailer(match_scores, winner_seat, num_players):
    """True — у текущей взятки игрок с наименьшим счётом; False — у лидера/выше мин."""
    scores = _match_score_list(match_scores, num_players)
    if winner_seat is None or not scores or winner_seat >= len(scores):
        return None
    w = scores[winner_seat]
    lo, hi = min(scores), max(scores)
    if lo == hi:
        return None
    if w <= lo:
        return True
    if w >= hi:
        return False
    return False  # «у кого больше», чем у аутсайдера → не кормим 10


def _pick_safest(
    cards,
    trump,
    seen,
    prefer_high_points=False,
    match_scores=None,
    winner_seat=None,
    num_players=4,
):
    """Сброс: беречь козырь/туза; 10 — аутсайдеру по счёту, даму — лидеру."""
    hint = prefer_high_points
    trailer = _trick_winner_is_trailer(match_scores, winner_seat, num_players)
    if trailer is True:
        hint = True
    elif trailer is False:
        hint = False

    def key(card):
        # При сбросе «на стол» туз масти уже не важен: 10 заберёт текущий победитель.
        unsafe = 0 if hint else (1 if _unsafe_ten(card, trump, seen) else 0)
        keep = _keep_priority(card, trump)
        pts = card_points(card, trump)
        if hint:
            # Кормим отстающего: 10 важнее дамы
            return (unsafe, -pts, keep)
        # Лидеру / «у кого больше» — мелочь (дама), не 10
        return (unsafe, pts, keep)

    return min(cards, key=key)


def choose_heuristic(state, seat, match_scores, legal_mask) -> int:
    legal = [int(a) for a in np.flatnonzero(legal_mask)]
    if not legal:
        raise ValueError("act() called with an empty legal mask")
    hand = list(state.hands[seat]) if 0 <= seat < len(state.hands) else []
    if getattr(state, "phase", "") == "BIDDING":
        return _bid(state, hand, legal, match_scores)
    return _play(state, seat, hand, legal, match_scores)


def _bid(state, hand, legal, match_scores):
    # Принять козырь: длина + сила масти + лицо
    if 33 in legal:
        length, points = _strength(hand, state.face_up_suit, extra=state.face_up_card)
        jacks = sum(1 for c in hand if c % 8 == 4 and c // 8 == state.face_up_suit)
        if state.face_up_card is not None and state.face_up_card % 8 == 4:
            jacks += 1
        strong = (length >= 3 and points >= 20) or length >= 4 or (length >= 3 and jacks)
        if strong:
            return 33
        return 32 if 32 in legal else legal[0]

    suits = [a - 34 for a in legal if 34 <= a <= 37]
    if suits:
        def suit_score(suit):
            length, points = _strength(hand, suit)
            jacks = sum(1 for c in hand if c // 8 == suit and c % 8 == 4)
            nines = sum(1 for c in hand if c // 8 == suit and c % 8 == 2)
            return (length, points + 20 * jacks + 10 * nines)

        best = max(suits, key=suit_score)
        length, points = _strength(hand, best)
        if 32 in legal and not ((length >= 3 and points >= 18) or length >= 4):
            return 32
        return 34 + best
    return 32 if 32 in legal else legal[0]


def _cheapest_winner(winners, trump, led_suit, seen):
    """Минимальная победа: козырная сила верная; 10 под туза не жечь."""
    safe = [c for c in winners if not _unsafe_ten(c, trump, seen)] or winners
    return min(safe, key=lambda c: (
        _play_power(c, trump, led_suit),
        card_points(c, trump),
        _keep_priority(c, trump),
    ))


def _smallest_trump(cards, trump):
    """Самый слабый козырь из доступных (сила J>9>A>10>K>Q)."""
    if trump is None:
        return None
    trumps = [c for c in cards if c // 8 == trump]
    if not trumps:
        return None
    return min(trumps, key=lambda c: (_T_POWER[c % 8], _T_PTS[c % 8], c))


def _smallest_follow(cards, trump):
    """Самая мелкая из легальных: сила в своей масти, затем очки."""
    def key(c):
        suit, rank = c // 8, c % 8
        if trump is not None and suit == trump:
            return (_T_POWER[rank], _T_PTS[rank], c)
        return (_NT_POWER[rank], _NT_PTS[rank], c)

    return min(cards, key=key)


def _dump_under_enemy_trump(cards, winners, trump, led_suit, current, partner_winning) -> int | None:
    """Ход в некозырную масть уже перекрыт козырем соперника — кидаем мелочь масти.

    Пример: валет червей → туз козырный → наше самое мелкое черво.
    """
    if (
        winners
        or partner_winning
        or trump is None
        or current is None
        or led_suit == trump
        or current // 8 != trump
        or not cards
        or not all(c // 8 == led_suit for c in cards)
    ):
        return None
    return _smallest_follow(cards, trump)


def _answer_high_or_queen(cards, winners, trump, led_suit, current, seen):
    """Спецправила добора в козыре.

    - На валет / 9 / туз: если не перебиваем — самый мелкий козырь (не жжём крупных).
    - На даму / короля: ставим самый слабый бьющий (король на даму, потом 10…),
      а не сразу самый большой (туз/валет).
    - Если отвечаем в масть хода под чужой козырь — самое мелкое в масти.
    """
    if current is None or trump is None:
        return None
    suit = current // 8
    rank = current % 8
    if suit != trump:
        return None

    if rank in (RANK_J, RANK_9, RANK_A):
        if winners:
            return _cheapest_winner(winners, trump, led_suit, seen)
        if led_suit != trump and cards and all(c // 8 == led_suit for c in cards):
            return _smallest_follow(cards, trump)
        small = _smallest_trump(cards, trump)
        return small if small is not None else _pick_safest(cards, trump, seen)

    if rank in (RANK_Q, RANK_K) and winners:
        # Минимальная победа: на даму — король (или следующий по силе), не туз зря
        return _cheapest_winner(winners, trump, led_suit, seen)
    return None


def _play(state, seat, hand, legal, match_scores):
    cards = [a for a in legal if 0 <= a <= 31]
    if not cards:
        return legal[0]

    trump = state.trump
    trick = list(state.current_trick or [])
    seats = int(getattr(state, "num_players", 4) or 4)
    team_play = seats == 4
    seen = _seen_cards(state, seat, hand)

    if not trick:
        return _lead(cards, hand, trump, state, seat)

    led_suit = trick[0][1] // 8
    current = _current_winner_card(trick, trump)
    leader = trick_winner_seat(trick, trump)
    partner_winning = team_play and leader is not None and (leader % 2) == (seat % 2)
    remaining = seats - len(trick)
    last = remaining <= 1
    winners = [c for c in cards if _beats(c, current, trump, led_suit)]

    # Партнёр уже выигрывает: не перекрывать без нужды
    if partner_winning:
        if last or not _opponents_can_overtake(state, seat, trick, trump, led_suit, current):
            # Очки партнёру: 10 — если он отстаёт; даму — если он лидер
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
        table_pts = sum(card_points(c, trump) for _, c in trick)
        ace_threat = (
            led_suit != trump
            and _suit_ace_out(led_suit, seen)
            and not last
        )
        if table_pts >= 10 or remaining == 1:
            # Бить сильнее, но 10 под возможный туз — только если других нет
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

    # Не берём: 10 — тому, у кого меньше очков партии; даму — у кого больше
    return _pick_safest(
        cards, trump, seen,
        match_scores=match_scores, winner_seat=leader, num_players=seats,
    )


def _play_power(card, trump, led_suit):
    suit = card // 8
    if trump is not None and suit == trump:
        return (2, _T_POWER[card % 8])
    if suit == led_suit:
        return (1, _NT_POWER[card % 8])
    return (0, _NT_POWER[card % 8])


def _opponents_can_overtake(state, seat, trick, trump, led_suit, current) -> bool:
    """Грубая оценка: остались ли соперники, которые ещё не ходили и могут бить."""
    seats = int(getattr(state, "num_players", 4) or 4)
    played = {s for s, _ in trick}
    remaining_seats = [(seat + k) % seats for k in range(1, seats) if (seat + k) % seats not in played]
    # Убрать себя если уже в списке
    remaining_seats = [s for s in remaining_seats if s != seat]
    if seats == 4:
        opponents = [s for s in remaining_seats if (s % 2) != (seat % 2)]
    else:
        opponents = list(remaining_seats)
    if not opponents or current is None:
        return True
    voids = _known_voids(state, seat)
    grave = set(state.graveyard or [])
    for _, c in trick:
        grave.add(c)
    # Если все оппоненты void в масти хода и void в козыре — риск низкий
    for opp in opponents:
        vs = voids.get(opp, set())
        can_follow = led_suit not in vs
        can_trump = trump is not None and trump not in vs
        if can_follow or can_trump:
            return True
    return False


def _lead(cards, hand, trump, state, seat):
    # Не вести мелким козырем без нужды; не ходить с 10, пока туз масти гуляет.
    seen = _seen_cards(state, seat, hand)

    def lead_score(card):
        suit = card // 8
        is_trump = trump is not None and suit == trump
        length = sum(1 for c in hand if c // 8 == suit)
        pts = card_points(card, trump)
        # В козыре сила: J > 9 > A > 10 > K > Q (не путать с обычной мастью)
        power = _T_POWER[card % 8] if is_trump else _NT_POWER[card % 8]
        trump_pen = 4 if is_trump and power < 6 else 0
        risk = _lead_penalty(card, trump, seen)
        return (power - trump_pen - risk, length, pts)

    return max(cards, key=lead_score)


class StrongAgent:
    name = "strong"

    def __init__(self, checkpoint=None, time_budget_ms=800, **_ignored):
        self.model = None
        self.model_version = None
        self.strategy = "heuristic"
        self._time_budget_ms = float(time_budget_ms or 800)
        path = (checkpoint or "").strip()
        if path:
            self._load_model(path)
        else:
            # Активная модель из реестра, если есть
            try:
                from ..learning.registry import active_checkpoint
                active = active_checkpoint()
                if active:
                    self._load_model(str(active))
            except Exception:
                pass

    def _load_model(self, path: str) -> None:
        from ..learning.model import ActionModel
        model = ActionModel.load(path)
        self.model = model
        self.model_version = model.version
        self.strategy = f"model:{model.version}"

    def reset(self):
        return None

    def snapshot(self):
        return None

    def restore(self, snapshot):
        return None

    def describe(self) -> str:
        return f"strong/{self.strategy}"

    def act(self, state, seat, match_scores, legal_mask):
        legal = np.asarray(legal_mask, dtype=np.int8)
        if not legal.any():
            raise ValueError("act() called with an empty legal mask")

        if self.model is not None:
            t0 = time.monotonic()
            try:
                action = self._act_model(state, seat, match_scores, legal)
                elapsed_ms = (time.monotonic() - t0) * 1000
                if elapsed_ms > self._time_budget_ms:
                    # Бюджет превышен — эвристика (модель уже посчитала, но
                    # следующий раз не должны зависать на тяжёлом пути)
                    pass
                if legal[action]:
                    return int(action)
            except Exception:
                pass
            # Резерв: эвристика
            return choose_heuristic(state, seat, match_scores, legal)

        return choose_heuristic(state, seat, match_scores, legal)

    def _act_model(self, state, seat, match_scores, legal):
        from ..learning.features import encode_state
        x = encode_state(state, seat, match_scores, legal)
        scores = self.model.predict_scores(x)
        # Только legal
        masked = np.where(legal.astype(bool), scores, -1e9)
        return int(np.argmax(masked))


@register("strong")
def _build(checkpoint=None, time_budget_ms=800, **kwargs):
    return StrongAgent(
        checkpoint=checkpoint,
        time_budget_ms=time_budget_ms,
        **kwargs,
    )
