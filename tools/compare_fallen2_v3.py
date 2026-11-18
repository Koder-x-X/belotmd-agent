"""Сравнение Strong / Fallen / fallen22 / fallen2(v3) на состояниях из журналов.

Не запускает самоигру. Берёт decision.state из freeze/review данных.
Пишет ≥20 отличающихся и контрольных примеров в Markdown+JSON.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from belotmd.agents import get_agent
from belotmd.game.state import BelotState

HIST = ROOT / "desk" / "history"
OUT = ROOT / "desk" / "history" / "fallen2_v3_compare"
SUITS = "♦♥♣♠"
RANKS = "789TJQKA"


def card_label(c):
    if c is None:
        return None
    c = int(c)
    return f"{RANKS[c % 8]}{SUITS[c // 8]}"


def state_from_decision(dec: dict, meta: dict | None = None) -> BelotState | None:
    st_raw = dec.get("state")
    if not isinstance(st_raw, dict):
        return None
    seat = dec.get("seat")
    if seat is None:
        return None
    seat = int(seat)
    hands_raw = st_raw.get("hands") or []
    if not isinstance(hands_raw, list) or seat >= len(hands_raw):
        return None
    mine = hands_raw[seat]
    if not isinstance(mine, dict) or not mine.get("known"):
        return None
    cards = mine.get("cards")
    if not isinstance(cards, list) or not cards:
        return None
    n = int(st_raw.get("num_players") or (meta or {}).get("variant") or 3)
    if n not in (3, 4):
        n = 3
    state = BelotState()
    state.num_players = n
    state.phase = "PLAYING"
    state.trump = st_raw.get("trump")
    state.declarer = st_raw.get("declarer")
    state.declarer_has_played_trump = bool(
        st_raw.get("trumpOpen", st_raw.get("declarer_has_played_trump", True))
    )
    state.current_player = seat
    trick = st_raw.get("current_trick") or dec.get("tableBefore") or []
    norm = []
    for p in trick:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            norm.append((int(p[0]), int(p[1])))
        elif isinstance(p, dict) and p.get("card") is not None:
            norm.append((int(p.get("seat", 0)), int(p["card"])))
    state.current_trick = norm
    state.graveyard = [int(c) for c in (st_raw.get("graveyard") or []) if isinstance(c, int)]
    state.hands = [[] for _ in range(4)]
    own = [int(c) for c in cards]
    state.hands[seat] = own
    # согласованные размеры рук для детерминизации: |hands| = 32 - grave - trick
    in_hands = 32 - len(state.graveyard) - len(norm)
    if in_hands < len(own):
        return None
    opp_total = in_hands - len(own)
    others = [i for i in range(n) if i != seat]
    # длины из state, если есть; иначе равномерно
    sizes = {}
    known_sum = 0
    unknown = []
    for i in others:
        h = hands_raw[i] if i < len(hands_raw) else {}
        if not isinstance(h, dict):
            h = {}
        if h.get("count") is not None:
            sizes[i] = max(0, int(h["count"]))
            known_sum += sizes[i]
        else:
            unknown.append(i)
    if known_sum > opp_total:
        sizes = {}
        unknown = list(others)
        known_sum = 0
    rest = opp_total - known_sum
    if unknown:
        base, extra = divmod(max(0, rest), len(unknown))
        for j, i in enumerate(unknown):
            sizes[i] = base + (1 if j < extra else 0)
    elif known_sum != opp_total and others:
        # подправить последнего
        sizes[others[-1]] = max(0, sizes.get(others[-1], 0) + (opp_total - known_sum))
    for i in others:
        state.hands[i] = list(range(sizes.get(i, 0)))  # dummy ids; sampler uses length only
    state.impossible_cards = np.zeros((4, 32), dtype=bool)
    state.known_cards = np.zeros((4, 32), dtype=bool)
    for c in own:
        if 0 <= c < 32:
            state.known_cards[seat, c] = True
    # voids from decision if present
    voids = st_raw.get("voids") or st_raw.get("impossible_suits")
    if isinstance(voids, dict):
        for sk, suits in voids.items():
            try:
                si = int(sk)
            except (TypeError, ValueError):
                continue
            for suit in suits or []:
                for r in range(8):
                    state.impossible_cards[si, int(suit) * 8 + r] = True
    return state


def legal_mask_for(state: BelotState, dec: dict) -> np.ndarray:
    legal = state.get_legal_actions().astype(bool)
    la = dec.get("legalActions") or dec.get("legal_actions")
    if isinstance(la, list) and la:
        m = np.zeros(38, dtype=bool)
        for a in la:
            if isinstance(a, int) and 0 <= a < 38:
                m[a] = True
        # пересечение с engine legal
        return m & legal if legal.any() else m
    return legal


def load_play_decisions(limit_files: int = 5) -> list[dict]:
    freeze = HIST / "review_freeze_v21"
    paths: list[Path] = []
    if freeze.is_dir():
        # последний snapshot с журналами
        snaps = sorted(freeze.glob("snapshot_*"), reverse=True)
        for snap in snaps:
            found = sorted(snap.glob("*.jsonl")) or sorted(snap.glob("**/*.jsonl"))
            if found:
                paths = found
                break
        if not paths:
            paths = sorted(freeze.glob("**/*.jsonl"))
    if not paths:
        bots = HIST / "bots"
        paths = sorted(bots.glob("*/games/*.jsonl"))[:limit_files]
    else:
        paths = paths[:limit_files]
    out = []
    for path in paths:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "decision":
                continue
            if ev.get("phase") not in ("PLAYING", "PLAY", None) and ev.get("kind") not in ("play", None):
                # keep play-like
                if ev.get("chosenAction") is None or int(ev.get("chosenAction", -1)) > 31:
                    continue
            if not isinstance(ev.get("state"), dict):
                continue
            if int(ev.get("chosenAction") or ev.get("action") or 99) > 31:
                continue
            ev["_src"] = path.name
            ev["_line"] = line_no
            out.append(ev)
    return out


def run_compare(max_examples: int = 24) -> dict:
    agents = {
        "strong": get_agent("strong"),
        "fallen": get_agent("fallen"),
        "fallen22": get_agent("fallen22"),
        "fallen2": get_agent("fallen2", time_budget_ms=250, search_ms=120, n_worlds=6, margin_pts=2.0),
    }
    decisions = load_play_decisions()
    rows = []
    cats = Counter()
    for dec in decisions:
        if len(rows) >= max_examples * 3:
            break
        state = state_from_decision(dec)
        if state is None or state.trump is None:
            continue
        seat = int(dec["seat"])
        legal = legal_mask_for(state, dec)
        cards = [int(a) for a in np.flatnonzero(legal) if 0 <= int(a) <= 31]
        if len(cards) < 1:
            continue
        # ensure our cards intersect legal
        if not any(c in state.hands[seat] for c in cards):
            continue
        picks = {}
        ok = True
        for name, agent in agents.items():
            try:
                # fresh legal each time
                a = int(agent.act(state, seat, [0] * state.num_players, legal.copy()))
                if a not in cards and not legal[a]:
                    ok = False
                    break
                picks[name] = a
            except Exception:
                ok = False
                break
        if not ok:
            continue
        differ = len(set(picks.values())) > 1
        # classify situation
        trick = state.current_trick or []
        n = state.num_players
        last = len(trick) == n - 1
        tag = "single" if len(cards) == 1 else (
            "last_trick_play" if last else "mid_or_lead"
        )
        if differ:
            cats["differ"] += 1
        else:
            cats["same"] += 1
            if len(rows) > max_examples and not differ:
                continue
        search = getattr(agents["fallen2"], "last_search", None)
        rows.append({
            "src": dec.get("_src"),
            "line": dec.get("_line"),
            "handId": dec.get("handId"),
            "seat": seat,
            "trump": state.trump,
            "trumpLabel": SUITS[state.trump] if state.trump is not None else None,
            "hand": [card_label(c) for c in state.hands[seat]],
            "table": [{"seat": s, "card": card_label(c)} for s, c in trick],
            "legal": [card_label(c) for c in cards],
            "picks": {k: card_label(v) for k, v in picks.items()},
            "picksIds": picks,
            "differ": differ,
            "tag": tag,
            "fallen2": {
                "rule": (agents["fallen2"].last_decision or {}).get("rule_id"),
                "reason": (agents["fallen2"].last_decision or {}).get("reason"),
                "baseline": card_label((agents["fallen2"].last_decision or {}).get("baselineAction")),
                "search": None if search is None else {
                    "overridden": search.overridden,
                    "worlds": search.worlds_used,
                    "elapsedMs": round(search.elapsed_ms, 1),
                    "fallback": search.fallback,
                    "evals": [
                        {"card": card_label(e.card), "ev": round(e.expected, 2), "n": e.n_worlds}
                        for e in (search.evals or [])[:6]
                    ],
                },
            },
        })
        if sum(1 for r in rows if r["differ"]) >= max_examples:
            break

    # ensure diversity tags
    OUT.mkdir(parents=True, exist_ok=True)
    # prefer differ rows first
    rows.sort(key=lambda r: (0 if r["differ"] else 1, r.get("line") or 0))
    rows = rows[: max(max_examples, 20)]
    report = {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "method": "fallen2_v3 expected points vs strong/fallen/fallen22",
        "counts": dict(cats),
        "n": len(rows),
        "examples": rows,
        "notes": [
            "Оценка EV приблизительная (сэмпл + strong playout).",
            "Фактический исход журнала не доказывает альтернативу.",
            "Fallen не изменялся; fallen2=v3; fallen22=архив v2.2.",
        ],
    }
    (OUT / "compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = ["# Сравнение стратегий на состояниях из журналов (Fallen2 v3)", ""]
    md.append(f"Примеров: {len(rows)}. Отличающихся среди просмотренных: {cats.get('differ', 0)}.")
    md.append("")
    for i, r in enumerate(rows, 1):
        md.append(f"## {i}. {r['tag']} {'≠' if r['differ'] else '='} — {r['src']}:{r['line']}")
        md.append(f"- козырь: {r['trumpLabel']}, seat={r['seat']}, hand={r['hand']}")
        md.append(f"- стол: {r['table']}")
        md.append(f"- legal: {r['legal']}")
        md.append(f"- выборы: {r['picks']}")
        f2 = r["fallen2"]
        md.append(f"- fallen2: {f2.get('rule')} baseline={f2.get('baseline')} — {f2.get('reason')}")
        if f2.get("search"):
            md.append(f"- search: {f2['search']}")
        md.append("")
    (OUT / "COMPARE.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"ok": True, "n": len(rows), "differSeen": cats.get("differ", 0), "out": str(OUT)}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    run_compare(24)
