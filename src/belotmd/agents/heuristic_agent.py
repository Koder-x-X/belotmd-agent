"""Greedy belot: bid on real trump length, take a trick cheaply, else dump low.

This is the fixed reference player from BelotAI, measured at about +5.5 game
points per hand against uniform random play. It needs no checkpoint. On a
3-player table there is no partner, so a winning opponent is never fed points.
The legal mask from the bridge is authoritative.
"""

import numpy as np

from .base import register

_NT_POWER = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4, 6: 5, 3: 6, 7: 7}
_T_POWER = {0: 0, 1: 1, 5: 2, 6: 3, 3: 4, 7: 5, 2: 6, 4: 7}
_T_PTS = {0: 0, 1: 0, 2: 14, 3: 10, 4: 20, 5: 3, 6: 4, 7: 11}
_NT_PTS = {0: 0, 1: 0, 2: 0, 3: 10, 4: 2, 5: 3, 6: 4, 7: 11}


def _pw(card, trump):
    if trump is not None and card // 8 == trump:
        return (1, _T_POWER[card % 8])
    return (0, _NT_POWER[card % 8])


def _pt(card, trump):
    if trump is not None and card // 8 == trump:
        return _T_PTS[card % 8]
    return _NT_PTS[card % 8]


def _strength(hand, suit, extra=None):
    cards = [card for card in hand if card // 8 == suit]
    if extra is not None and extra // 8 == suit:
        cards.append(extra)
    return len(cards), sum(_T_PTS[card % 8] for card in cards)


def choose(state, seat, legal_mask):
    legal = [int(action) for action in np.flatnonzero(legal_mask)]
    if not legal:
        raise ValueError("act() called with an empty legal mask")
    hand = list(state.hands[seat]) if 0 <= seat < len(state.hands) else []
    if getattr(state, "phase", "") == "BIDDING":
        return _bid(state, hand, legal)
    return _play(state, seat, legal)


def _bid(state, hand, legal):
    if 33 in legal:
        length, points = _strength(hand, state.face_up_suit, extra=state.face_up_card)
        if (length >= 3 and points >= 20) or length >= 4:
            return 33
        return 32 if 32 in legal else legal[0]
    suits = [action - 34 for action in legal if 34 <= action <= 37]
    if suits:
        best = max(suits, key=lambda suit: _strength(hand, suit))
        length, points = _strength(hand, best)
        if 32 in legal and not (length >= 3 and points >= 20):
            return 32
        return 34 + best
    return 32 if 32 in legal else legal[0]


def _play(state, seat, legal):
    cards = [action for action in legal if 0 <= action <= 31]
    if not cards:
        return legal[0]
    trump = state.trump
    trick = list(state.current_trick or [])
    seats = int(getattr(state, "num_players", 4) or 4)
    if not trick:
        return max(
            cards,
            key=lambda card: _pw(card, trump)[1] - (3 if trump is not None and card // 8 == trump and _T_POWER[card % 8] < 6 else 0),
        )
    current = max(_pw(card, trump) for _, card in trick)
    leader = max(trick, key=lambda played: _pw(played[1], trump))[0]
    partner = seats == 4 and (leader % 2) == (seat % 2)
    winners = [card for card in cards if _pw(card, trump) > current]
    last = len(trick) >= seats - 1
    if partner and len(trick) >= 2:
        if last:
            return max(cards, key=lambda card: (_pt(card, trump), -_pw(card, trump)[1]))
        return min(cards, key=lambda card: (_pt(card, trump), _pw(card, trump)[1]))
    if winners:
        if last:
            return min(winners, key=lambda card: _pw(card, trump)[1])
        return max(winners, key=lambda card: _pw(card, trump)[1])
    return min(cards, key=lambda card: (_pt(card, trump), _pw(card, trump)[1]))


class HeuristicAgent:
    name = "heuristic"

    def reset(self):
        return None

    def snapshot(self):
        return None

    def restore(self, snapshot):
        return None

    def act(self, state, seat, match_scores, legal_mask):
        return choose(state, seat, legal_mask)


@register("heuristic")
def _build(**_ignored):
    return HeuristicAgent()
