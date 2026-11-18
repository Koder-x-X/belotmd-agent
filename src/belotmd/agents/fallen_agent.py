"""Агент Fallen: правила пользователя поверх strong.

Приоритет:
1. legal_mask / сервер
2. точное правило Fallen
3. актуальный план следующего хода
4. общие предпочтения Fallen
5. базовая стратегия strong (эвристика или модель)

Модель strong вызывается только если Fallen не выбрал ход.
Частные примеры не превращаются в безусловные правила.
Основной фокус — игра втроём; в других режимах — только однозначные случаи.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np

from .base import register
from .strong_agent import (
    RANK_9,
    RANK_10,
    RANK_A,
    RANK_J,
    RANK_K,
    RANK_Q,
    StrongAgent,
    _NT_POWER,
    _T_POWER,
    _beats,
    _current_winner_card,
    _smallest_trump,
    card_points,
    trick_winner_seat,
)

# ---------------------------------------------------------------------------
# Типы решения
# ---------------------------------------------------------------------------


@dataclass
class NextPlan:
    deal_id: int
    rule_id: str
    card: Optional[int] = None
    suit: Optional[int] = None
    reason: str = ""
    need_won_last: bool = True
    need_lead: bool = True
    require_trump_open: Optional[bool] = None  # None = не важно
    conditions: dict = field(default_factory=dict)


@dataclass
class Decision:
    action: int
    rule_id: str
    reason: str
    source: str = "fallen"  # fallen | strong | plan
    plan: Optional[NextPlan] = None
    cancel_plan: Optional[str] = None
    conflict: Optional[str] = None


# ---------------------------------------------------------------------------
# Память раздачи
# ---------------------------------------------------------------------------


class DealMemory:
    def __init__(self):
        self.deal_id = 0
        self.plan: Optional[NextPlan] = None
        self.last_log: Optional[dict] = None
        self.ambiguous: list[str] = []

    def new_deal(self) -> None:
        self.deal_id += 1
        self.plan = None
        self.ambiguous = []


# ---------------------------------------------------------------------------
# Утилиты карт / позиции
# ---------------------------------------------------------------------------


def _rank(card: int) -> int:
    return card % 8


def _suit(card: int) -> int:
    return card // 8


def _is_trump(card: int, trump) -> bool:
    return trump is not None and _suit(card) == trump


def _trump_power(card: int) -> int:
    return _T_POWER[_rank(card)]


def _plain_power(card: int) -> int:
    return _NT_POWER[_rank(card)]


def _seats(state) -> int:
    return int(getattr(state, "num_players", 4) or 4)


def _is_opponent(me: int, other: int, seats: int) -> bool:
    if seats != 4:
        return other != me
    return (other % 2) != (me % 2)


def _three_player(state) -> bool:
    return _seats(state) == 3


def _legal_cards(legal_mask) -> list[int]:
    return [int(a) for a in np.flatnonzero(legal_mask) if 0 <= int(a) <= 31]


def _hand(state, seat) -> list[int]:
    hands = getattr(state, "hands", None) or []
    if 0 <= seat < len(hands):
        return list(hands[seat])
    return []


def _led_suit(trick) -> Optional[int]:
    if not trick:
        return None
    return _suit(trick[0][1])


def _we_are_last(trick, seats: int) -> bool:
    return len(trick) == seats - 1


def _partner_winning(state, seat, trump) -> bool:
    seats = _seats(state)
    if seats != 4:
        return False
    winner = trick_winner_seat(state.current_trick or [], trump)
    if winner is None:
        return False
    return not _is_opponent(seat, winner, seats)


def _opponent_led(state, seat) -> bool:
    trick = state.current_trick or []
    if not trick:
        return False
    return _is_opponent(seat, trick[0][0], _seats(state))


def _card_in(cards, suit, rank) -> Optional[int]:
    want = suit * 8 + rank
    return want if want in cards else None


def _suits_in(cards) -> set[int]:
    return {_suit(c) for c in cards}


def _of_suit(cards, suit) -> list[int]:
    return [c for c in cards if _suit(c) == suit]


def _trumps(cards, trump) -> list[int]:
    if trump is None:
        return []
    return _of_suit(cards, trump)


def _strongest_trump(cards, trump) -> Optional[int]:
    ts = _trumps(cards, trump)
    if not ts:
        return None
    return max(ts, key=lambda c: (_trump_power(c), card_points(c, trump), c))


def _weakest_plain(cards) -> Optional[int]:
    if not cards:
        return None
    return min(cards, key=lambda c: (_plain_power(c), card_points(c, None), c))


def _would_win(card, trick, trump) -> bool:
    current = _current_winner_card(trick, trump)
    led = _led_suit(trick)
    if led is None:
        return True
    return _beats(card, current, trump, led)


def _trick_ranks(trick, suit) -> set[int]:
    return {_rank(c) for _, c in trick if _suit(c) == suit}


def _no_ruff_on_table(trick, trump) -> bool:
    if trump is None:
        return True
    led = _led_suit(trick)
    if led == trump:
        return True
    return not any(_is_trump(c, trump) for _, c in trick)


def _deal_fingerprint(state, seat) -> tuple:
    hand = tuple(sorted(_hand(state, seat)))
    return (
        getattr(state, "trump", None),
        getattr(state, "declarer", None),
        getattr(state, "tricks_played", 0),
        len(getattr(state, "graveyard", None) or []),
        hand,
    )


def _last_trick_winner(state) -> Optional[int]:
    last = getattr(state, "last_trick", None) or []
    if not last:
        return None
    return trick_winner_seat(last, getattr(state, "trump", None))


def _trump_open(state) -> bool:
    return bool(getattr(state, "declarer_has_played_trump", False))


def _fmt_card(card: int) -> str:
    suits = ("♦", "♥", "♣", "♠")
    ranks = {0: "7", 1: "8", 2: "9", 3: "10", 4: "J", 5: "Q", 6: "K", 7: "A"}
    return f"{ranks.get(_rank(card), '?')}{suits[_suit(card)] if 0 <= _suit(card) < 4 else '?'}"


# ---------------------------------------------------------------------------
# Правила Fallen
# ---------------------------------------------------------------------------


def _rule_f01(ctx) -> Optional[Decision]:
    """Соперник вышел козырным валетом → младший разрешённый козырь."""
    trick, trump, cards, seat = ctx["trick"], ctx["trump"], ctx["cards"], ctx["seat"]
    if not trick or trump is None or not _opponent_led(ctx["state"], seat):
        return None
    lead = trick[0][1]
    if not (_is_trump(lead, trump) and _rank(lead) == RANK_J):
        return None
    small = _smallest_trump(cards, trump)
    if small is None or small not in cards:
        return None
    return Decision(
        small,
        "F01",
        f"соперник вышел козырным валетом — отдаём младший разрешённый козырь {_fmt_card(small)}",
    )


def _rule_f02(ctx) -> Optional[Decision]:
    """Козырная дама соперника, у нас туз+валет → туз, план валет."""
    trick, trump, cards, seat, mem = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["seat"], ctx["mem"]
    )
    if not trick or trump is None or not _opponent_led(ctx["state"], seat):
        return None
    lead = trick[0][1]
    if not (_is_trump(lead, trump) and _rank(lead) == RANK_Q):
        return None
    ace = _card_in(cards, trump, RANK_A)
    jack = _card_in(cards, trump, RANK_J)
    if ace is None or jack is None:
        return None
    if not _would_win(ace, trick, trump):
        return None
    plan = NextPlan(
        deal_id=mem.deal_id,
        rule_id="F02",
        card=jack,
        reason="после туза на даму — выход козырным валетом",
        require_trump_open=True,
    )
    return Decision(
        ace,
        "F02",
        f"на козырную даму ставим туза {_fmt_card(ace)}, валета сохраняем",
        plan=plan,
    )


def _rule_f03(ctx) -> Optional[Decision]:
    """Козырная дама, у нас король+девятка → король если выигрывает."""
    trick, trump, cards, seat = ctx["trick"], ctx["trump"], ctx["cards"], ctx["seat"]
    if not trick or trump is None or not _opponent_led(ctx["state"], seat):
        return None
    lead = trick[0][1]
    if not (_is_trump(lead, trump) and _rank(lead) == RANK_Q):
        return None
    king = _card_in(cards, trump, RANK_K)
    nine = _card_in(cards, trump, RANK_9)
    if king is None or nine is None:
        return None
    # Если кто-то уже перебил даму сильнее короля — не ставим короля впустую
    if not _would_win(king, trick, trump):
        return None
    return Decision(
        king,
        "F03",
        f"на козырную даму ставим короля {_fmt_card(king)}, девятку сохраняем",
    )


def _rule_ruff_pairs(ctx) -> Optional[Decision]:
    """Перебивание обычной масти козырем (раздел 7). Не ответ на козырный ход."""
    trick, trump, cards, state, seat = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["state"], ctx["seat"]
    )
    if not trick or trump is None:
        return None
    led = _led_suit(trick)
    if led is None or led == trump:
        return None
    # Только когда нет масти хода, а козыри в legal
    if any(_suit(c) == led for c in cards):
        return None
    ts = _trumps(cards, trump)
    if not ts:
        return None

    # Уже перебито чужим козырем — проверяем силу
    current = _current_winner_card(trick, trump)
    winning_trumps = [c for c in ts if _would_win(c, trick, trump)]

    ranks = {_rank(c) for c in ts}
    jack = _card_in(cards, trump, RANK_J)
    ace = _card_in(cards, trump, RANK_A)
    ten = _card_in(cards, trump, RANK_10)
    nine = _card_in(cards, trump, RANK_9)
    king = _card_in(cards, trump, RANK_K)

    pick = None
    rid = "Ruff"
    reason = ""

    # Точные пары (сохраняем валета, если есть другой козырь для перебития)
    if jack is not None and ace is not None and ace in (winning_trumps or ts):
        if ace in winning_trumps or (not winning_trumps and _would_win(ace, trick, trump)):
            pick, rid, reason = ace, "Ruff-JA", f"валет+туз → туз {_fmt_card(ace)}"
    elif jack is not None and ten is not None and (ten in winning_trumps or _would_win(ten, trick, trump)):
        pick, rid, reason = ten, "Ruff-J10", f"валет+десятка → десятка {_fmt_card(ten)}"
    elif jack is not None and nine is not None and (nine in winning_trumps or _would_win(nine, trick, trump)):
        pick, rid, reason = nine, "Ruff-J9", f"валет+девятка → девятка {_fmt_card(nine)}"
    elif ace is not None and nine is not None and jack is None:
        if nine in winning_trumps or _would_win(nine, trick, trump):
            pick, rid, reason = nine, "Ruff-A9", f"туз+девятка → девятка {_fmt_card(nine)}"
    elif king is not None and ace is not None and jack is None:
        if ace in winning_trumps or _would_win(ace, trick, trump):
            pick, rid, reason = ace, "Ruff-KA", f"король+туз → туз {_fmt_card(ace)}"

    if pick is None and jack is None:
        # Нет валета — самый сильный разрешённый козырь, который бьёт (если надо)
        pool = winning_trumps if (
            current is not None and _is_trump(current, trump)
        ) else ts
        # Если перебивать обязательно по legal — все ts уже legal
        if current is not None and _is_trump(current, trump):
            pool = winning_trumps
            if not pool:
                return None
        pick = _strongest_trump(pool, trump)
        rid, reason = "Ruff-Max", f"без валета — сильнейший козырь {_fmt_card(pick)}" if pick else ""

    if pick is None and jack is not None and winning_trumps:
        # Валет есть, но точная пара не сработала — другие бьющие без валета
        others = [c for c in winning_trumps if c != jack]
        if others:
            # Предпочитаем слабейший бьющий, сохраняя валета
            pick = min(others, key=lambda c: (_trump_power(c), c))
            rid, reason = "Ruff-KeepJ", f"сохраняем валета, бьём {_fmt_card(pick)}"
        elif jack in winning_trumps and len(winning_trumps) == 1:
            # Только валет бьёт
            pick = jack
            rid, reason = "Ruff-OnlyJ", f"бьём единственным валетом {_fmt_card(jack)}"

    if pick is None or pick not in cards:
        return None
    if current is not None and _is_trump(current, trump) and not _would_win(pick, trick, trump):
        return None
    return Decision(pick, rid, reason)


def _rule_last_plain(ctx) -> Optional[Decision]:
    """F04–F09: ход последним в некозырную масть без чужого козыря на столе."""
    trick, trump, cards, state, seat, mem = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["state"], ctx["seat"], ctx["mem"]
    )
    seats = _seats(state)
    if not trick or not _we_are_last(trick, seats):
        return None
    led = _led_suit(trick)
    if led is None or (trump is not None and led == trump):
        return None
    if not _no_ruff_on_table(trick, trump):
        return None
    follow = _of_suit(cards, led)
    if not follow:
        return None

    ranks_on = _trick_ranks(trick, led)
    our_ranks = {_rank(c) for c in follow}

    # F04: стол J+Q, у нас 9+10 → 10, план 9
    if ranks_on == {RANK_J, RANK_Q} and {RANK_9, RANK_10}.issubset(our_ranks):
        ten = _card_in(follow, led, RANK_10)
        nine = _card_in(follow, led, RANK_9)
        if ten and _would_win(ten, trick, trump):
            return Decision(
                ten, "F04",
                f"забираем десяткой {_fmt_card(ten)}, следующий ход — девятка",
                plan=NextPlan(mem.deal_id, "F04", card=nine,
                              reason="девятка после десятки на J+Q"),
            )

    # F05: стол Q+J, у нас K+A → K, план A
    if ranks_on == {RANK_J, RANK_Q} and {RANK_K, RANK_A}.issubset(our_ranks):
        king = _card_in(follow, led, RANK_K)
        ace = _card_in(follow, led, RANK_A)
        if king and _would_win(king, trick, trump):
            return Decision(
                king, "F05",
                f"забираем королём {_fmt_card(king)}, сохраняем туза для следующего выхода",
                plan=NextPlan(mem.deal_id, "F05", card=ace,
                              reason="туз после короля на Q+J — вытянуть десятку"),
            )

    # F06: стол Q+J, у нас 10+K → 10, план K
    if ranks_on == {RANK_J, RANK_Q} and {RANK_10, RANK_K}.issubset(our_ranks):
        ten = _card_in(follow, led, RANK_10)
        king = _card_in(follow, led, RANK_K)
        if ten and _would_win(ten, trick, trump):
            return Decision(
                ten, "F06",
                f"забираем десяткой {_fmt_card(ten)}, следующий — король",
                plan=NextPlan(mem.deal_id, "F06", card=king,
                              reason="король после десятки на Q+J"),
            )

    # F09: стол Q+J, у нас K+9 → K
    if ranks_on == {RANK_J, RANK_Q} and {RANK_K, RANK_9}.issubset(our_ranks):
        king = _card_in(follow, led, RANK_K)
        if king and _would_win(king, trick, trump):
            return Decision(
                king, "F09",
                f"забираем королём {_fmt_card(king)} (дама+валет на столе)",
            )

    # F07: стол K+9 → туз, иначе десятка
    if ranks_on == {RANK_K, RANK_9}:
        ace = _card_in(follow, led, RANK_A)
        ten = _card_in(follow, led, RANK_10)
        if ace and _would_win(ace, trick, trump):
            return Decision(ace, "F07", f"на король+девятку ставим туза {_fmt_card(ace)}")
        if ten and _would_win(ten, trick, trump):
            return Decision(ten, "F07", f"на король+девятку ставим десятку {_fmt_card(ten)}")
        return None  # иначе strong

    # F08: стол 9+10 → туз
    if ranks_on == {RANK_9, RANK_10}:
        ace = _card_in(follow, led, RANK_A)
        if ace and _would_win(ace, trick, trump):
            return Decision(ace, "F08", f"на девятку+десятку ставим туза {_fmt_card(ace)}")
        return None

    return None


def _rule_f10(ctx) -> Optional[Decision]:
    """Король+дама на столе, у нас A+10+J → забрать тузом очки сейчас."""
    trick, trump, cards, state, seat = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["state"], ctx["seat"]
    )
    seats = _seats(state)
    if not trick or not _we_are_last(trick, seats):
        return None
    led = _led_suit(trick)
    if led is None or (trump is not None and led == trump):
        return None
    if not _no_ruff_on_table(trick, trump):
        return None
    if _trick_ranks(trick, led) != {RANK_K, RANK_Q}:
        return None
    follow = _of_suit(cards, led)
    if not {RANK_A, RANK_10, RANK_J}.issubset({_rank(c) for c in follow}):
        return None
    ace = _card_in(follow, led, RANK_A)
    if ace and _would_win(ace, trick, trump):
        return Decision(
            ace, "F10",
            f"король+дама на столе — забираем туза {_fmt_card(ace)} сейчас (риск козыря)",
        )
    return None


def _rule_plain_ace_response(ctx) -> Optional[Decision]:
    """Ответ на некозырного туза: младшая карта масти, беречь десятку."""
    trick, trump, cards, seat = ctx["trick"], ctx["trump"], ctx["cards"], ctx["seat"]
    if not trick or not _opponent_led(ctx["state"], seat):
        return None
    lead = trick[0][1]
    if _is_trump(lead, trump) or _rank(lead) != RANK_A:
        return None
    led = _suit(lead)
    follow = _of_suit(cards, led)
    if not follow:
        return None  # void → ruff/discard rules elsewhere
    # Младшая, предпочитая не десятку если есть другая
    non_ten = [c for c in follow if _rank(c) != RANK_10]
    pool = non_ten or follow
    pick = _weakest_plain(pool)
    if pick is None:
        return None
    return Decision(
        pick, "AceDump",
        f"на некозырного туза отдаём младшую {_fmt_card(pick)}, десятку бережём",
    )


def _rule_discard_no_trump(ctx) -> Optional[Decision]:
    """F11–F13: сброс, когда соперник ходит козырем, а у нас козырей нет."""
    trick, trump, cards, state, seat = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["state"], ctx["seat"]
    )
    if not trick or trump is None:
        return None
    led = _led_suit(trick)
    if led != trump:
        return None
    if _trumps(cards, trump):
        return None
    if not cards:
        return None

    # Группировка по мастям
    by_suit: dict[int, list[int]] = {}
    for c in cards:
        by_suit.setdefault(_suit(c), []).append(c)

    # F12: в масти 10+J+Q → сбросить валета
    for suit, sc in by_suit.items():
        ranks = {_rank(c) for c in sc}
        if {RANK_10, RANK_J, RANK_Q}.issubset(ranks):
            jack = _card_in(sc, suit, RANK_J)
            if jack is not None:
                # Если лид — туз этой масти, и есть дама — отдаём даму (часть F12)
                lead = trick[0][1]
                if _suit(lead) != trump and _rank(lead) == RANK_A and _suit(lead) == suit:
                    queen = _card_in(sc, suit, RANK_Q)
                    if queen is not None:
                        return Decision(
                            queen, "F12",
                            f"на туза масти отдаём даму {_fmt_card(queen)}, десятку бережём",
                        )
                return Decision(
                    jack, "F12",
                    f"сброс: из 10+J+Q отдаём валета {_fmt_card(jack)}, бережём 10 и даму",
                )

    # F13: туз+король+девятка → сначала девятка
    for suit, sc in by_suit.items():
        ranks = {_rank(c) for c in sc}
        if {RANK_A, RANK_K, RANK_9}.issubset(ranks):
            nine = _card_in(sc, suit, RANK_9)
            if nine is not None:
                return Decision(
                    nine, "F13",
                    f"сброс: девятка {_fmt_card(nine)} из туз+король+девятка",
                )

    # F11: беречь десятку с прикрытием — сброс из другой масти
    tens = [c for c in cards if _rank(c) == RANK_10]
    for ten in tens:
        suit = _suit(ten)
        cover = [c for c in by_suit.get(suit, []) if c != ten]
        if not cover:
            continue
        # Есть сброс в другой масти?
        other = [c for c in cards if _suit(c) != suit]
        if other:
            # Не сбрасываем единственную десятку другой масти без нужды
            pick = min(
                other,
                key=lambda c: (
                    1 if _rank(c) == RANK_10 else 0,
                    1 if _rank(c) == RANK_A else 0,
                    _plain_power(c),
                    card_points(c, trump),
                    c,
                ),
            )
            return Decision(
                pick, "F11",
                f"бережём десятку {_fmt_card(ten)} с прикрытием — сброс {_fmt_card(pick)}",
            )

    return None


def _rule_lead_prefs(ctx) -> Optional[Decision]:
    """Выход в обычную масть и связанные планы (раздел 12) + пример §13."""
    trick, trump, cards, state, seat, mem = (
        ctx["trick"], ctx["trump"], ctx["cards"], ctx["state"], ctx["seat"], ctx["mem"]
    )
    if trick:
        return None  # только выход
    if not cards:
        return None

    # §13: рука J♥ + 9♥ + A♣, козырь ♥ — если можно козырем: 9, план J затем A♣
    # Срабатывает как подтверждённый пример при точном составе (и возможно после взятия)
    if trump is not None and _three_player(state):
        j_t = _card_in(cards, trump, RANK_J)
        n_t = _card_in(cards, trump, RANK_9)
        # Тузы некозырные
        plain_aces = [c for c in cards if _rank(c) == RANK_A and not _is_trump(c, trump)]
        if j_t and n_t and len(cards) <= 4 and plain_aces:
            # Если козыри открыты или мы декларатор — можно выйти козырем
            can_trump_lead = (
                _trump_open(state)
                or seat == getattr(state, "declarer", None)
                or all(_is_trump(c, trump) for c in cards)
            )
            # Пример: после взятия девяткой — валет; но на выходе с этими картами
            # и если только что выиграли козырной девяткой — план уже мог быть.
            # Здесь: выход 9 если разрешён и есть J+9+ace
            if can_trump_lead and n_t in cards and j_t in cards:
                ace = plain_aces[0]
                return Decision(
                    n_t, "P13",
                    f"план §13: выход козырной девяткой {_fmt_card(n_t)}, далее валет, затем {_fmt_card(ace)}",
                    plan=NextPlan(
                        mem.deal_id, "P13", card=j_t,
                        reason="валет после девятки (§13)",
                        require_trump_open=True,
                        conditions={"then_card": ace},
                    ),
                )
            if not can_trump_lead and plain_aces:
                ace = plain_aces[0]
                return Decision(
                    ace, "P13",
                    f"козыри ещё закрыты — выход тузом {_fmt_card(ace)} (§13)",
                )

    # Масть только туз+десятка → туз, план десятка
    for suit in range(4):
        if trump is not None and suit == trump:
            continue
        sc = _of_suit(cards, suit)
        if len(sc) == 2 and {_rank(c) for c in sc} == {RANK_A, RANK_10}:
            ace = _card_in(sc, suit, RANK_A)
            ten = _card_in(sc, suit, RANK_10)
            if ace in cards:
                return Decision(
                    ace, "Lead-A10",
                    f"в масти только туз+десятка — выход тузом {_fmt_card(ace)}",
                    plan=NextPlan(
                        mem.deal_id, "Lead-A10", card=ten,
                        reason="десятка после туза",
                    ),
                )

    # Десятка с сопровождением — предпочитать сопровождение
    for suit in range(4):
        if trump is not None and suit == trump:
            continue
        sc = _of_suit(cards, suit)
        if len(sc) >= 2 and any(_rank(c) == RANK_10 for c in sc):
            companions = [c for c in sc if _rank(c) != RANK_10]
            if companions:
                # Не делаем безусловным для любой ситуации — только если нет
                # более сильного специального правила выше. Здесь — мягкое
                # предпочтение при единственной такой масти среди лидов.
                pick = _weakest_plain(companions)
                if pick is not None and len([s for s in range(4)
                                            if s != trump and len(_of_suit(cards, s)) >= 2
                                            and any(_rank(c) == RANK_10 for c in _of_suit(cards, s))]) == 1:
                    return Decision(
                        pick, "Lead-Keep10",
                        f"выход сопровождающей {_fmt_card(pick)}, десятку бережём",
                    )

    return None


def _rule_followup_p13_ace(ctx) -> Optional[Decision]:
    """Вторая ступень плана §13: после валета — туз (если план conditions.then_card)."""
    # Обрабатывается через общий plan executor + chaining в act()
    return None


# Порядок проверки специальных правил (конфликт → strong)
_SPECIAL_RULES = [
    _rule_f01,
    _rule_f02,
    _rule_f03,
    _rule_plain_ace_response,
    _rule_f10,
    _rule_last_plain,
    _rule_ruff_pairs,
    _rule_discard_no_trump,
    _rule_lead_prefs,
]


# ---------------------------------------------------------------------------
# Агент
# ---------------------------------------------------------------------------


class FallenAgent:
    name = "fallen"
    agent_version = "1.0.0"

    def __init__(self, checkpoint=None, time_budget_ms=800, **kwargs):
        # База strong — для неописанных ситуаций (модель только там).
        self._base = StrongAgent(
            checkpoint=checkpoint,
            time_budget_ms=time_budget_ms,
            **kwargs,
        )
        self.model_version = getattr(self._base, "model_version", None)
        self.mem = DealMemory()
        self.last_decision: Optional[dict] = None
        self._fp: Optional[tuple] = None
        self._pending_chain: Optional[NextPlan] = None

    def describe(self) -> str:
        base = self._base.describe()
        last = (self.last_decision or {}).get("rule_id") or "idle"
        return f"fallen/{last}|{base}"

    def reset(self) -> None:
        self.mem.new_deal()
        self._fp = None
        self._pending_chain = None
        self.last_decision = None
        self._base.reset()

    def snapshot(self):
        return {
            "deal_id": self.mem.deal_id,
            "plan": replace(self.mem.plan) if self.mem.plan else None,
            "pending_chain": replace(self._pending_chain) if self._pending_chain else None,
            "fp": self._fp,
            "ambiguous": list(self.mem.ambiguous),
            "base": self._base.snapshot(),
        }

    def restore(self, snapshot) -> None:
        if not snapshot:
            return
        self.mem.deal_id = snapshot.get("deal_id", self.mem.deal_id)
        self.mem.plan = snapshot.get("plan")
        self.mem.ambiguous = list(snapshot.get("ambiguous") or [])
        self._pending_chain = snapshot.get("pending_chain")
        self._fp = snapshot.get("fp")
        self._base.restore(snapshot.get("base"))

    def _ensure_deal(self, state, seat) -> None:
        fp = _deal_fingerprint(state, seat)
        if self._fp is None:
            self._fp = fp
            return
        # Новая раздача: рука выросла / трюк обнулился с другим козырем
        if fp[0] != self._fp[0] or fp[1] != self._fp[1]:
            self.mem.new_deal()
            self._pending_chain = None
        elif fp[2] < self._fp[2]:
            self.mem.new_deal()
            self._pending_chain = None
        self._fp = fp

    def _try_plan(self, ctx) -> Optional[Decision]:
        plan = self.mem.plan
        if plan is None:
            return None
        if plan.deal_id != self.mem.deal_id:
            self.mem.plan = None
            ctx["plan_cancel"] = "план от другой раздачи"
            return None
        cancel = None
        if plan.need_lead and ctx["trick"]:
            cancel = "план только на выход в новую взятку"
        elif plan.need_won_last:
            winner = _last_trick_winner(ctx["state"])
            if winner is not None and winner != ctx["seat"]:
                cancel = "предыдущую взятку выиграли не мы"
        if cancel is None and plan.require_trump_open is True and not _trump_open(ctx["state"]):
            if ctx["seat"] != getattr(ctx["state"], "declarer", None):
                cancel = "козыри ещё не открыты"
        card = plan.card
        if cancel is None and card is not None and card not in ctx["cards"]:
            cancel = f"карты плана {_fmt_card(card)} нет в руке/legal"
        if cancel:
            self.mem.plan = None
            ctx["plan_cancel"] = cancel
            return None
        if card is not None and card in ctx["cards"]:
            then = (plan.conditions or {}).get("then_card")
            next_plan = None
            if then is not None:
                next_plan = NextPlan(
                    deal_id=self.mem.deal_id,
                    rule_id=plan.rule_id + "+",
                    card=int(then),
                    reason=f"продолжение плана после {_fmt_card(card)}",
                )
            self.mem.plan = None
            return Decision(
                card, plan.rule_id,
                f"выполняем план: {_fmt_card(card)} ({plan.reason})",
                source="plan",
                plan=next_plan,
            )
        self.mem.plan = None
        ctx["plan_cancel"] = "план без конкретной карты — сброс"
        return None

    def _collect_rules(self, ctx) -> list[Decision]:
        hits = []
        for fn in _SPECIAL_RULES:
            # В 4p — только однозначные правила без «соперник=партнёр»
            if not _three_player(ctx["state"]):
                # Разрешаем last-plain, f10, ruff, discard, lead prefs;
                # F01-F03 и AceDump — только если лидер реально соперник
                if fn in (_rule_f01, _rule_f02, _rule_f03, _rule_plain_ace_response):
                    if not _opponent_led(ctx["state"], ctx["seat"]):
                        continue
            try:
                d = fn(ctx)
            except Exception:
                continue
            if d is not None and d.action in ctx["legal_set"]:
                hits.append(d)
        return hits

    def _log(self, decision: Decision, card_action: int) -> None:
        rec = {
            "agent": "Fallen",
            "source": decision.source,
            "rule_id": decision.rule_id,
            "action": card_action,
            "card": _fmt_card(card_action) if 0 <= card_action <= 31 else str(card_action),
            "reason": decision.reason,
            "plan": None,
            "cancel_plan": decision.cancel_plan,
            "conflict": decision.conflict,
        }
        if decision.plan is not None:
            rec["plan"] = {
                "rule_id": decision.plan.rule_id,
                "card": _fmt_card(decision.plan.card) if decision.plan.card is not None else None,
                "reason": decision.plan.reason,
            }
        self.last_decision = rec
        plan_s = ""
        if rec["plan"]:
            plan_s = f" | plan={rec['plan']['card']} ({rec['plan']['reason']})"
        cancel_s = f" | cancel={decision.cancel_plan}" if decision.cancel_plan else ""
        conflict_s = f" | CONFLICT={decision.conflict}" if decision.conflict else ""
        print(
            f"[Fallen] {decision.rule_id} | source={decision.source} | "
            f"{rec['card']} | {decision.reason}{plan_s}{cancel_s}{conflict_s}",
            flush=True,
        )

    def act(self, state, seat, match_scores, legal_mask) -> int:
        legal = np.asarray(legal_mask, dtype=np.int8)
        if not legal.any():
            raise ValueError("act() called with an empty legal mask")

        self._ensure_deal(state, seat)

        # Торги — всегда strong
        if getattr(state, "phase", "") == "BIDDING":
            action = self._base.act(state, seat, match_scores, legal)
            d = Decision(action, "BID", "торги — базовая стратегия strong", source="strong")
            self._log(d, action)
            return int(action)

        cards = _legal_cards(legal)
        legal_set = set(int(a) for a in np.flatnonzero(legal))
        ctx = {
            "state": state,
            "seat": seat,
            "trump": getattr(state, "trump", None),
            "trick": list(getattr(state, "current_trick", None) or []),
            "cards": cards,
            "hand": _hand(state, seat),
            "mem": self.mem,
            "legal_set": legal_set,
            "match_scores": match_scores,
            "plan_cancel": None,
        }

        # 1) План
        planned = self._try_plan(ctx)
        if planned is not None and planned.action in legal_set:
            if planned.plan is not None:
                self.mem.plan = planned.plan
            if ctx["plan_cancel"]:
                planned.cancel_plan = ctx["plan_cancel"]
            self._log(planned, planned.action)
            return int(planned.action)

        # 2) Специальные правила
        hits = self._collect_rules(ctx)
        if len(hits) > 1:
            # Конфликт — не гадаем
            ids = ",".join(sorted({h.rule_id for h in hits}))
            action = self._base.act(state, seat, match_scores, legal)
            d = Decision(
                action, "CONFLICT",
                f"конфликт правил [{ids}] — безопасный выбор strong",
                source="strong",
                conflict=ids,
                cancel_plan=ctx.get("plan_cancel"),
            )
            self.mem.ambiguous.append(ids)
            self._log(d, action)
            return int(action)

        if len(hits) == 1:
            d = hits[0]
            if ctx.get("plan_cancel"):
                d.cancel_plan = ctx["plan_cancel"]
            if d.plan is not None:
                self.mem.plan = d.plan
            self._log(d, d.action)
            return int(d.action)

        # 3) Strong
        action = self._base.act(state, seat, match_scores, legal)
        d = Decision(
            action, "STRONG",
            "правило Fallen не найдено — решение strong",
            source="strong",
            cancel_plan=ctx.get("plan_cancel"),
        )
        self._log(d, action)
        return int(action)


@register("fallen")
def _build(checkpoint=None, time_budget_ms=800, **kwargs):
    return FallenAgent(
        checkpoint=checkpoint,
        time_budget_ms=time_budget_ms,
        **kwargs,
    )
