"""Агент Fallen3: Fallen2 EV + mid-trick EXP из уроков pack + более глубокий поиск.

База — Fallen2 v3 (ожидаемые очки). Усиления из desk/history + lessons:
- EXP-Take/Cheap не только последним ходом (как fallen21) — в pack много
  «не взяли, хотя могли побить» в середине взятки;
- ниже порог смены решения и больше миров/времени на EV;
- в эндшпиле ещё глубже.

Торговлю не меняем. Скрытые руки не читаем. При сбое поиска — Fallen+EXP.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np

from .base import register
from .fallen_agent import (
    Decision,
    FallenAgent,
    _fmt_card,
    _is_trump,
    _legal_cards,
    _partner_winning,
    _plain_power,
    _seats,
    _three_player,
    _trump_power,
    _we_are_last,
    _would_win,
)
from .fallen2_points import (
    SearchReport,
    evaluate_legal_moves,
    pick_with_margin,
)
from .strong_agent import StrongAgent, card_points


VERSION = "3.1.2"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _beater_cost(card: int, trump) -> tuple:
    if _is_trump(card, trump):
        power = _trump_power(card)
    else:
        power = _plain_power(card)
    return (card_points(card, trump), power, card)


def _rule_exp_take_cheap_mid(ctx) -> Optional[Decision]:
    """Уроки pack: бить стол дешёвой бьющей на любом ответе (не только last)."""
    trick = ctx["trick"]
    trump = ctx["trump"]
    cards = ctx["cards"]
    state = ctx["state"]
    seat = ctx["seat"]
    if not trick or not cards:
        return None
    if not _three_player(state) and _partner_winning(state, seat, trump):
        return None

    beaters = [c for c in cards if _would_win(c, trick, trump)]
    if not beaters:
        return None

    pick = min(beaters, key=lambda c: _beater_cost(c, trump))
    non_beaters = [c for c in cards if c not in beaters]
    cheaper_than_max = any(
        _beater_cost(pick, trump) < _beater_cost(c, trump) for c in beaters
    )
    last = _we_are_last(trick, _seats(state))

    # Mid-trick: срабатываем если иначе сбросили бы / есть дешевле среди бьющих.
    if not non_beaters and not cheaper_than_max and not last:
        return None

    where = "последний" if last else "ответ"
    if non_beaters:
        rid, reason = "EXP3-Take", (
            f"опыт pack ({where}): забираем {_fmt_card(pick)}, не сбрасываем"
        )
    elif cheaper_than_max and len(beaters) > 1:
        rid, reason = "EXP3-Cheap", (
            f"опыт pack ({where}): дешевле {_fmt_card(pick)} без переплаты"
        )
    else:
        rid, reason = "EXP3-Take", (
            f"опыт pack ({where}): бьём {_fmt_card(pick)}"
        )
    return Decision(pick, rid, reason)


class Fallen3Agent(FallenAgent):
    name = "fallen3"
    agent_version = VERSION

    def __init__(
        self,
        checkpoint=None,
        time_budget_ms=1000,
        margin_pts=None,
        n_worlds=None,
        search_ms=None,
        **kwargs,
    ):
        super().__init__(
            checkpoint=checkpoint,
            time_budget_ms=time_budget_ms,
            **kwargs,
        )
        self.time_budget_ms = float(time_budget_ms or 1000)
        self.model_version = f"fallen3-{VERSION}"
        # Ниже margin → чаще доверяем EV, когда pack/Fallen расходятся.
        self.margin_pts = float(
            margin_pts
            if margin_pts is not None
            else _env_float("FALLEN3_MARGIN_PTS", 1.0)
        )
        self.n_worlds = int(
            n_worlds if n_worlds is not None else _env_int("FALLEN3_WORLDS", 16)
        )
        self.search_ms = float(
            search_ms
            if search_ms is not None
            else _env_float("FALLEN3_SEARCH_MS", 520)
        )
        self.last_search: SearchReport | None = None
        self._playout = StrongAgent(checkpoint=None, time_budget_ms=50)

    def describe(self) -> str:
        base = self._base.describe()
        last = (self.last_decision or {}).get("rule_id") or "idle"
        return f"fallen3/{VERSION}/{last}|{base}"

    def _collect_rules(self, ctx):
        # Last-trick cheap beater beats Fallen hard rules: pack review showed
        # almost all «возможная_ошибка» were last-trick overpays.
        try:
            exp = _rule_exp_take_cheap_mid(ctx)
        except Exception:
            exp = None
        if (
            exp is not None
            and exp.action in ctx["legal_set"]
            and exp.rule_id == "EXP3-Cheap"
            and _we_are_last(ctx["trick"], _seats(ctx["state"]))
        ):
            return [exp]
        hits = super()._collect_rules(ctx)
        if hits:
            return hits
        if exp is not None and exp.action in ctx["legal_set"]:
            hits.append(exp)
        return hits

    def _baseline_act(self, state, seat, match_scores, legal_mask) -> int:
        """Fallen + EXP3; last-trick overpay forced to cheapest beater."""
        action = int(super().act(state, seat, match_scores, legal_mask))
        try:
            ctx_cards = _legal_cards(np.asarray(legal_mask, dtype=np.int8))
            trick = list(getattr(state, "current_trick", None) or [])
            trump = getattr(state, "trump", None)
            if not trick or not _we_are_last(trick, _seats(state)):
                return action
            if not _three_player(state) and _partner_winning(state, seat, trump):
                return action
            beaters = [c for c in ctx_cards if _would_win(c, trick, trump)]
            if len(beaters) < 2:
                return action
            cheap = min(beaters, key=lambda c: _beater_cost(c, trump))
            if action in beaters and _beater_cost(cheap, trump) < _beater_cost(
                action, trump
            ):
                self._log(
                    Decision(
                        cheap,
                        "EXP3-Cheap",
                        f"анти-переплата (последний): {_fmt_card(cheap)} "
                        f"вместо {_fmt_card(action)}",
                    ),
                    cheap,
                )
                return int(cheap)
        except Exception:
            return action
        return action

    def act(self, state, seat, match_scores, legal_mask) -> int:
        legal = np.asarray(legal_mask, dtype=np.int8)
        if not legal.any():
            raise ValueError("act() called with an empty legal mask")

        if getattr(state, "phase", "") == "BIDDING":
            return self._baseline_act(state, seat, match_scores, legal)

        cards = _legal_cards(legal)
        if len(cards) <= 1:
            action = cards[0] if cards else int(np.flatnonzero(legal)[0])
            baseline = action
            self._finish_log(
                action,
                baseline,
                SearchReport(
                    baseline=baseline,
                    chosen=action,
                    margin=self.margin_pts,
                    overridden=False,
                    reason="единственный допустимый ход",
                    evals=[],
                    worlds_used=0,
                    elapsed_ms=0.0,
                ),
            )
            return int(action)

        t0 = time.monotonic()
        baseline = self._baseline_act(state, seat, match_scores, legal)
        baseline_dec = dict(self.last_decision or {})

        budget = min(self.search_ms, max(60.0, float(self.time_budget_ms) * 0.65))
        hand_n = len(state.hands[seat]) if 0 <= seat < len(state.hands) else 8
        worlds = self.n_worlds + (8 if hand_n <= 3 else (4 if hand_n <= 5 else 0))

        evals, used, fallback = evaluate_legal_moves(
            state,
            seat,
            cards,
            policy=self._playout.act,
            n_worlds=worlds,
            time_budget_ms=budget,
        )

        if not evals or all(e.n_worlds == 0 for e in evals):
            action = baseline
            report = SearchReport(
                baseline=baseline,
                chosen=action,
                margin=self.margin_pts,
                overridden=False,
                reason=f"резерв Fallen+EXP ({fallback or 'no_eval'})",
                evals=[],
                worlds_used=0,
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                fallback=fallback or "no_eval",
            )
            self._finish_log(action, baseline, report, baseline_dec)
            return int(action)

        action, overridden, reason = pick_with_margin(
            evals, baseline, self.margin_pts
        )
        if fallback == "heuristic":
            reason = f"эвристика (нет сэмпла): {reason}"
        if action not in cards:
            action = baseline
            overridden = False
            reason = "кандидат вне legal — Fallen+EXP"
        # Hard anti-overpay when we are last and already winning the trick.
        try:
            trick = list(getattr(state, "current_trick", None) or [])
            trump = getattr(state, "trump", None)
            if trick and _we_are_last(trick, _seats(state)):
                if _three_player(state) or not _partner_winning(state, seat, trump):
                    beaters = [c for c in cards if _would_win(c, trick, trump)]
                    if len(beaters) >= 2:
                        cheap = min(beaters, key=lambda c: _beater_cost(c, trump))
                        if action in beaters and action != cheap:
                            reason = (
                                f"анти-переплата: {_fmt_card(cheap)} вместо "
                                f"{_fmt_card(action)} (последний)"
                            )
                            action = int(cheap)
                            overridden = action != baseline
        except Exception:
            pass
        report = SearchReport(
            baseline=baseline,
            chosen=action,
            margin=self.margin_pts,
            overridden=overridden,
            reason=reason,
            evals=evals,
            worlds_used=used,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
            fallback=fallback,
        )
        self._finish_log(action, baseline, report, baseline_dec)
        return int(action)

    def _finish_log(
        self,
        action: int,
        baseline: int,
        report: SearchReport,
        baseline_dec: dict | None = None,
    ) -> None:
        self.last_search = report
        alts = [
            {
                "card": e.card,
                "label": _fmt_card(e.card),
                "expected": round(e.expected, 2),
                "std": round(e.std, 2),
                "n": e.n_worlds,
            }
            for e in report.evals
            if e.n_worlds > 0
        ]
        alts.sort(key=lambda x: -x["expected"])
        rid = "EV-Override" if report.overridden else "EV-Keep"
        # heuristic still ranked cards — don't brand it as hard fallback
        if (
            report.fallback
            and report.fallback != "heuristic"
            and not report.overridden
        ):
            rid = "EV-Fallback"
        reason = report.reason
        if report.overridden:
            reason = (
                f"Выбрана {_fmt_card(action)}. {report.reason}. "
                f"{report.approx_note}"
            )
        d = Decision(
            action,
            rid,
            reason,
            source="fallen3-ev" if report.overridden else "fallen3",
        )
        self._log(d, action)
        if self.last_decision is not None:
            self.last_decision["agent"] = "Fallen3"
            self.last_decision["agentVersion"] = VERSION
            self.last_decision["baselineAction"] = baseline
            self.last_decision["baselineCard"] = _fmt_card(baseline)
            self.last_decision["baselineRule"] = (baseline_dec or {}).get("rule_id")
            self.last_decision["expectedPoints"] = alts
            self.last_decision["search"] = {
                "margin": report.margin,
                "overridden": report.overridden,
                "worlds": report.worlds_used,
                "elapsedMs": round(report.elapsed_ms, 1),
                "fallback": report.fallback,
                "approx": report.approx_note,
            }

    def _log(self, decision: Decision, card_action: int) -> None:
        super()._log(decision, card_action)
        if self.last_decision is not None:
            self.last_decision["agent"] = "Fallen3"
            self.last_decision["agentVersion"] = VERSION


@register("fallen3")
def _build(checkpoint=None, time_budget_ms=1000, **kwargs):
    return Fallen3Agent(
        checkpoint=checkpoint,
        time_budget_ms=time_budget_ms,
        **kwargs,
    )
