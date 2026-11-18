"""Сбор отчёта/примеров по зафиксированным журналам recorder 2.1.

ТЗ v3: история, анализатор rules-2.2, reconstruct 2.2, pack/keys, отчёт.
Не меняет стратегии. Строит belot_review_*_v3 из одной неизменяемой копии.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent
HIST = ROOT / "desk" / "history"
FREEZE_DIR = HIST / "review_freeze_v21"
REPORT_DIR = HIST / "review_pack_staging_v2"
CODE_ZIP = OUT / "belot_review_code_v3.zip"
DATA_ZIP = OUT / "belot_review_data_v3.zip"
MANIFEST = OUT / "belot_review_MANIFEST_v3.json"

SECRET_NAME_RE = re.compile(
    r"(admin\.token|\.env$|cookies?|credentials|password|secret|api[_-]?key|session\.json|profiles\.json)",
    re.I,
)
EXCLUDE_DIRS = {
    "node_modules", "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "dist", "build", ".tox",
}
SUITS = "♦♥♣♠"
RANKS = "789TJQKA"

# Контроль структуры журналов (не меняется анализатором). Оценки ошибок — отдельно.
EXPECTED_STRUCT = {
    "decisionRecords": 1007,
    "uniqueDecisions": 533,
    "playDecisions": 474,
    "playConfirmed": 474,
    "bidDecisions": 59,
    "plays": 1422,
    "handEndEvents": 69,
    "uniqueHandsWithEnd": 64,
}
# Ожидания reconstruct/оценки v2 — для COMPARISON, не блокируют ok сборки v3
EXPECTED_V2_QUALITY = {
    "completeHands": 50,
    "partialHands": 14,
    "possibleErrorsOur": 10,
    "possibleErrorsOpp": 20,
}


def first_defined(*vals: Any) -> Any:
    """Первое значение, которое не None (сохраняет 0 и False)."""
    for v in vals:
        if v is not None:
            return v
    return None


def card_label(cid: Any) -> str | None:
    if cid is None:
        return None
    try:
        c = int(cid)
    except (TypeError, ValueError):
        return None
    if not (0 <= c < 32):
        return "?"
    return f"{RANKS[c % 8]}{SUITS[c // 8]}"


def card_pair(cid: Any) -> dict | None:
    if cid is None:
        return None
    return {"id": cid, "label": card_label(cid)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_jsonl_strict(path: Path) -> tuple[list[dict], list[dict]]:
    """Читает JSONL; битые строки не пропускаются молча — в errors."""
    events: list[dict] = []
    errors: list[dict] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(raw.splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append({"file": str(path), "line": i, "error": str(exc), "excerpt": text[:120]})
            continue
        if not isinstance(obj, dict):
            errors.append({"file": str(path), "line": i, "error": "not_object"})
            continue
        obj = dict(obj)
        obj["_srcFile"] = path.name
        obj["_srcLine"] = i
        events.append(obj)
    return events, errors


def find_source_journals(input_data: Path | None = None) -> list[Path]:
    """Пять журналов: из data zip (v3/v2) или freeze/bots."""
    candidates = []
    if input_data is not None:
        candidates.append(Path(input_data))
    candidates.extend([
        OUT / "belot_review_data_v2.zip",
        OUT / "belot_review_data_v3.zip",
        OUT / "belot_review_data.zip",
    ])
    for data_zip in candidates:
        if not data_zip.is_file():
            continue
        staging = HIST / "_review_src_zip"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(data_zip, "r") as zf:
            names = [n for n in zf.namelist() if n.startswith("journals/") and n.endswith(".jsonl")]
            for n in names:
                target = staging / Path(n).name
                target.write_bytes(zf.read(n))
        paths = sorted(staging.glob("*.jsonl"))
        if len(paths) == 5:
            return paths
    # fallback: scan bots for recorder 2.1
    bots = HIST / "bots"
    found = []
    for path in sorted(bots.rglob("game_*.jsonl"), key=lambda p: p.stat().st_mtime):
        events, _ = read_jsonl_strict(path)
        meta = next((e for e in events if e.get("type") == "meta"), None)
        if not meta:
            continue
        rv = str(meta.get("recorderVersion") or "")
        sv = str(meta.get("schemaVersion") or "")
        if rv.startswith("2.1") or sv == "history-2.1":
            found.append(path)
    return found[:5] if len(found) >= 5 else found


def freeze_journals(sources: list[Path]) -> list[dict]:
    if FREEZE_DIR.exists():
        # keep previous freezes as dated dirs; clean working copy subtree
        for child in FREEZE_DIR.iterdir():
            if child.name.startswith("snapshot_"):
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    snap = FREEZE_DIR / f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    journals_dir = snap / "journals"
    journals_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    all_errors: list[dict] = []
    for src in sources:
        dest = journals_dir / src.name
        data = src.read_bytes()
        dest.write_bytes(data)
        events, errors = read_jsonl_strict(dest)
        all_errors.extend(errors)
        meta = next((e for e in events if e.get("type") == "meta"), {}) or {}
        ts = [float(e["t"]) for e in events if isinstance(e.get("t"), (int, float))]
        row = {
            "file": dest.name,
            "rel": f"journals/{dest.name}",
            "path": dest,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
            "eventCount": len(events),
            "parseErrors": errors,
            "gameId": meta.get("gameId"),
            "botId": meta.get("botId"),
            "login": meta.get("login"),
            "agent": meta.get("agent"),
            "agentVersion": meta.get("agentVersion"),
            "recorderVersion": meta.get("recorderVersion"),
            "schemaVersion": meta.get("schemaVersion"),
            "buildId": meta.get("buildId"),
            "matchKey": meta.get("matchKey"),
            "tMin": min(ts) if ts else None,
            "tMax": max(ts) if ts else None,
            "events": events,
        }
        rows.append(row)
    (snap / "freeze_index.json").write_text(
        json.dumps(
            [{k: v for k, v in r.items() if k != "events" and k != "path"} | {"path": r["rel"]} for r in rows],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (snap / "parse_errors.json").write_text(json.dumps(all_errors, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        r["snapshotDir"] = snap
    return rows


def decision_stage(ev: dict) -> str:
    if ev.get("serverConfirmed") or ev.get("confirmStatus") == "server_confirmed":
        return "server_confirmed"
    if ev.get("rejected") or ev.get("confirmStatus") == "rejected":
        return "rejected"
    if ev.get("confirmStatus") == "sent" or (ev.get("sendAttempts") or 0) > 0:
        return "sent"
    return "chosen"


def merge_decisions(game_id: str, events: list[dict]) -> list[dict]:
    """Объединяет записи одного decisionId в одно решение."""
    by_id: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for ev in events:
        if ev.get("type") != "decision":
            continue
        did = str(ev.get("decisionId") or "")
        if not did:
            continue
        if did not in by_id:
            order.append(did)
        by_id[did].append(ev)

    merged = []
    for did in order:
        stages = by_id[did]
        stages_sorted = sorted(stages, key=lambda e: first_defined(e.get("t"), 0.0))
        last = stages_sorted[-1]
        hand_ids = {str(e.get("handId")) for e in stages_sorted if e.get("handId") is not None}
        seats = {e.get("seat") for e in stages_sorted if e.get("seat") is not None}
        actions = {
            first_defined(e.get("confirmedAction"), e.get("chosenAction"), e.get("intentAction"))
            for e in stages_sorted
        }
        inconsistent = len(hand_ids) > 1 or len(seats) > 1 or (len(actions - {None}) > 1)
        statuses = [decision_stage(e) for e in stages_sorted]
        final = "chosen"
        if "server_confirmed" in statuses:
            final = "server_confirmed"
        elif "rejected" in statuses:
            final = "rejected"
        elif "sent" in statuses:
            final = "sent"
        # richest state: prefer last with state.hands
        state = {}
        state_src = None
        for e in reversed(stages_sorted):
            st = e.get("state")
            if isinstance(st, dict) and st:
                state = st
                state_src = {"file": e.get("_srcFile"), "line": e.get("_srcLine")}
                break
        phase = first_defined(last.get("phase"), stages_sorted[0].get("phase"))
        merged.append({
            "gameId": game_id,
            "decisionId": did,
            "phase": phase,
            "kind": "play" if phase == "PLAYING" else "bidding",
            "handId": first_defined(last.get("handId"), stages_sorted[0].get("handId")),
            "seat": first_defined(last.get("seat"), stages_sorted[0].get("seat")),
            "confirmStatus": final,
            "confirmed": final == "server_confirmed",
            "rejected": final == "rejected",
            "chosenAction": first_defined(last.get("chosenAction"), stages_sorted[0].get("chosenAction")),
            "confirmedAction": first_defined(last.get("confirmedAction"), last.get("chosenAction")),
            "intentAction": first_defined(last.get("intentAction"), stages_sorted[0].get("intentAction")),
            "legalActions": first_defined(last.get("legalActions"), stages_sorted[0].get("legalActions")),
            "ruleId": first_defined(last.get("ruleId"), stages_sorted[0].get("ruleId")),
            "reason": first_defined(last.get("reason"), stages_sorted[0].get("reason")),
            "agent": first_defined(last.get("agent"), stages_sorted[0].get("agent")),
            "state": state,
            "stateSource": state_src,
            "recordCount": len(stages_sorted),
            "stages": [{"status": decision_stage(e), "line": e.get("_srcLine"), "t": e.get("t")} for e in stages_sorted],
            "inconsistent": inconsistent,
            "sourceLines": [e.get("_srcLine") for e in stages_sorted],
            "sourceFile": stages_sorted[0].get("_srcFile"),
        })
    return merged


def seat_hand_info(state: dict, seat: Any) -> dict:
    out = {
        "seat": seat,
        "known": None,
        "complete": None,
        "cards": None,
        "count": None,
        "source": "decision.state.hands[seat]",
    }
    hands = state.get("hands") if isinstance(state, dict) else None
    if not isinstance(hands, list) or seat is None:
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
    cards = h.get("cards")
    count = h.get("count")
    out["known"] = known
    out["cards"] = cards if isinstance(cards, list) else None
    out["count"] = first_defined(count, len(cards) if isinstance(cards, list) else None)
    if known is True and isinstance(cards, list):
        out["complete"] = True
    elif known is False:
        out["complete"] = False
    else:
        out["complete"] = None
    return out


def trump_from_decision_or_events(decision: dict, events: list[dict]) -> dict:
    state = decision.get("state") if isinstance(decision.get("state"), dict) else {}
    trump = state.get("trump")
    source = "decision.state.trump"
    if trump is None:
        hid = decision.get("handId")
        for e in events:
            if e.get("type") == "trump" and str(e.get("handId")) == str(hid) and e.get("trump") is not None:
                trump = e.get("trump")
                source = f"event:trump line={e.get('_srcLine')}"
                break
            if e.get("type") == "hand_start" and str(e.get("handId")) == str(hid) and e.get("trump") is not None:
                trump = e.get("trump")
                source = f"event:hand_start line={e.get('_srcLine')}"
                break
    declarer = state.get("declarer")
    trump_open = first_defined(state.get("trumpOpen"), state.get("trump_open"))
    return {
        "trump": trump,
        "trumpLabel": {0: "♦", 1: "♥", 2: "♣", 3: "♠"}.get(trump) if isinstance(trump, int) else None,
        "declarer": declarer,
        "trumpOpen": trump_open,
        "source": source if trump is not None else None,
        "unknown": trump is None,
    }


def analyze_journals(journals: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    sys.path.insert(0, str(ROOT / "src"))
    from belotmd.history.analyze import analyze_game
    from belotmd.history.reconstruct import reconstruct_game

    decision_records = 0
    unique_decisions: list[dict] = []
    plays = 0
    hand_end_events = 0
    unique_hands_end: set[tuple] = set()
    repeat_hand_ends: list[dict] = []
    seen_hand_end: set[tuple] = set()

    complete_hands = 0
    partial_hands = 0
    partial_details: list[dict] = []
    partial_trick_hist = Counter()

    analyzed_moves: list[dict] = []
    parse_errors = []
    for j in journals:
        parse_errors.extend(j.get("parseErrors") or [])

    for j in journals:
        gid = j["gameId"]
        events = j["events"]
        decision_records += sum(1 for e in events if e.get("type") == "decision")
        plays += sum(1 for e in events if e.get("type") == "play")
        for e in events:
            if e.get("type") != "hand_end":
                continue
            hand_end_events += 1
            key = (gid, str(e.get("handId")))
            unique_hands_end.add(key)
            if key in seen_hand_end:
                repeat_hand_ends.append({"gameId": gid, "handId": e.get("handId"), "line": e.get("_srcLine")})
            else:
                seen_hand_end.add(key)

        merged = merge_decisions(gid, events)
        for d in merged:
            d["login"] = j.get("login")
            d["agent"] = d.get("agent") or j.get("agent")
            d["agentVersion"] = j.get("agentVersion")
            d["botId"] = j.get("botId")
            d["file"] = j["file"]
            unique_decisions.append(d)

        recon = reconstruct_game(events)
        for h in recon:
            status = h.get("status")
            tricks = h.get("tricks")
            n_tricks = len(tricks) if isinstance(tricks, list) else int(tricks or 0)
            if status == "complete":
                complete_hands += 1
            else:
                partial_hands += 1
                partial_trick_hist[n_tricks] += 1
                # try to classify early end vs data loss
                hid = h.get("handId")
                has_end = any(
                    e.get("type") == "hand_end" and str(e.get("handId")) == str(hid) for e in events
                )
                reason_server = "не установлено"
                if has_end and n_tricks < 8:
                    # look for hand_end payload hints
                    ends = [e for e in events if e.get("type") == "hand_end" and str(e.get("handId")) == str(hid)]
                    end0 = ends[0] if ends else {}
                    if end0.get("earlyEnd") or end0.get("capot") or end0.get("reason"):
                        reason_server = str(end0.get("reason") or end0.get("earlyEnd") or "server_early_end")
                    else:
                        reason_server = "возможно штатное досрочное окончание или потеря событий — не установлено"
                elif not has_end:
                    reason_server = "нет hand_end"
                partial_details.append({
                    "gameId": gid,
                    "handId": hid,
                    "status": status,
                    "trickCount": n_tricks,
                    "reasons": h.get("reasons") or h.get("contradictions") or [],
                    "classification": reason_server,
                    "file": j["file"],
                })

        moves = analyze_game(events, recon)
        for m in moves:
            m = dict(m)
            m["gameId"] = gid
            m["file"] = j["file"]
            m["login"] = j.get("login")
            m["agent"] = j.get("agent")
            m["agentVersion"] = j.get("agentVersion")
            # attach decision state for samples
            did = m.get("decisionId")
            linked = next((d for d in merged if d.get("decisionId") == did), None)
            m["_decision"] = linked
            analyzed_moves.append(m)

    play_dec = [d for d in unique_decisions if d.get("kind") == "play"]
    bid_dec = [d for d in unique_decisions if d.get("kind") == "bidding"]
    play_confirmed = [d for d in play_dec if d.get("confirmStatus") == "server_confirmed"]
    play_rejected = [d for d in play_dec if d.get("confirmStatus") == "rejected"]
    play_unconfirmed = [d for d in play_dec if d.get("confirmStatus") not in ("server_confirmed", "rejected")]
    bid_confirmed = [d for d in bid_dec if d.get("confirmStatus") == "server_confirmed"]
    bid_rejected = [d for d in bid_dec if d.get("confirmStatus") == "rejected"]
    bid_unconfirmed = [d for d in bid_dec if d.get("confirmStatus") not in ("server_confirmed", "rejected")]

    # trump unknown among unique play decisions
    unknown_trump_play_dec = 0
    incomplete_hand_play = 0
    for d in play_dec:
        info = seat_hand_info(d.get("state") or {}, d.get("seat"))
        if info.get("complete") is False or info.get("known") is False:
            incomplete_hand_play += 1
        tr = trump_from_decision_or_events(d, next(j["events"] for j in journals if j["gameId"] == d["gameId"]))
        if tr["unknown"]:
            unknown_trump_play_dec += 1

    # analyzed moves metrics
    unknown_trump_moves = 0  # analyzer top-level has no trump; use linked decision
    untrusted = 0
    trusted = 0
    legal_unknown = 0
    cats_our: Counter = Counter()
    cats_opp: Counter = Counter()
    possible_our = 0
    possible_opp = 0
    card_plays = [m for m in analyzed_moves if m.get("kind") == "card_play"]
    for m in card_plays:
        post = m.get("evalPostReveal") if isinstance(m.get("evalPostReveal"), dict) else {}
        at = m.get("evalAtTime") if isinstance(m.get("evalAtTime"), dict) else {}
        lt = first_defined(post.get("legalTrusted"), at.get("legalTrusted"))
        if lt is True:
            trusted += 1
        elif lt is False:
            untrusted += 1
        else:
            legal_unknown += 1
        # trump via decision
        dec = m.get("_decision") or {}
        tr = None
        if dec:
            tr = trump_from_decision_or_events(
                dec,
                next(j["events"] for j in journals if j["gameId"] == m["gameId"]),
            )
            if tr["unknown"]:
                unknown_trump_moves += 1
        cat = first_defined(post.get("category"), post.get("label"), "unknown")
        if m.get("us"):
            cats_our[cat] += 1
            if cat == "возможная_ошибка":
                possible_our += 1
        else:
            cats_opp[cat] += 1
            if cat == "возможная_ошибка":
                possible_opp += 1

    denom = len(card_plays) or 1
    # reliable = confirmed fact + trusted legal; contested = спорный/кандидат; insufficient = недостаточно
    reliable = sum(1 for m in card_plays if (m.get("evalPostReveal") or {}).get("category") == "подтверждённый_факт")
    contested = sum(
        1
        for m in card_plays
        if (m.get("evalPostReveal") or {}).get("category") in ("спорный_ход", "кандидат_на_разбор", "возможная_ошибка")
    )
    insufficient = sum(
        1 for m in card_plays if (m.get("evalPostReveal") or {}).get("category") == "недостаточно_данных"
    )

    quality = {
        "freezeHasParseErrors": bool(parse_errors),
        "parseErrors": parse_errors,
        "uniqueMatches": len({j.get("matchKey") for j in journals if j.get("matchKey")}),
        "participations": len(journals),
        "decisionEventRecords": decision_records,
        "uniqueDecisions": len(unique_decisions),
        "playDecisions": len(play_dec),
        "playConfirmed": len(play_confirmed),
        "playRejected": len(play_rejected),
        "playUnconfirmed": len(play_unconfirmed),
        "bidDecisions": len(bid_dec),
        "bidConfirmed": len(bid_confirmed),
        "bidRejected": len(bid_rejected),
        "bidUnconfirmed": len(bid_unconfirmed),
        "noteBidUnconfirmed": "неподтверждённую торговлю не считаем автоматически отклонённой",
        "plays": plays,
        "handEndEvents": hand_end_events,
        "uniqueHandsWithEnd": len(unique_hands_end),
        "repeatHandEnds": repeat_hand_ends,
        "completeHands": complete_hands,
        "partialHands": partial_hands,
        "partialTrickHistogram": {str(k): v for k, v in sorted(partial_trick_hist.items())},
        "partialDetails": partial_details,
        "unknownTrumpUniquePlayDecisions": unknown_trump_play_dec,
        "unknownTrumpAnalyzedMovesViaDecisionState": unknown_trump_moves,
        "incompleteOwnHandPlayDecisions": incomplete_hand_play,
        "legalTrustedTrue": trusted,
        "legalTrustedFalse": untrusted,
        "legalTrustedUnknown": legal_unknown,
        "legalTrustedNote": "отсутствие legalTrusted = неизвестно, не достоверно",
        "evalPostRevealCategoriesOur": dict(cats_our),
        "evalPostRevealCategoriesOpp": dict(cats_opp),
        "possibleErrorsOur": possible_our,
        "possibleErrorsOpp": possible_opp,
        "possibleErrorsNote": "метки анализатора evalPostReveal, не доказанные ошибки стратегии",
        "shareReliable": {"count": reliable, "denom": len(card_plays), "pct": round(100 * reliable / denom, 2)},
        "shareContested": {"count": contested, "denom": len(card_plays), "pct": round(100 * contested / denom, 2)},
        "shareInsufficient": {"count": insufficient, "denom": len(card_plays), "pct": round(100 * insufficient / denom, 2)},
        "analyzedCardPlays": len(card_plays),
        "controlCheck": {
            k: {"expected": EXPECTED_STRUCT[k], "actual": actual, "ok": actual == EXPECTED_STRUCT[k]}
            for k, actual in {
                "decisionRecords": decision_records,
                "uniqueDecisions": len(unique_decisions),
                "playDecisions": len(play_dec),
                "playConfirmed": len(play_confirmed),
                "bidDecisions": len(bid_dec),
                "plays": plays,
                "handEndEvents": hand_end_events,
                "uniqueHandsWithEnd": len(unique_hands_end),
            }.items()
        },
        "qualityDeltaVsV2": {
            k: {
                "v2": EXPECTED_V2_QUALITY[k],
                "v3": actual,
                "changed": actual != EXPECTED_V2_QUALITY[k],
            }
            for k, actual in {
                "completeHands": complete_hands,
                "partialHands": partial_hands,
                "possibleErrorsOur": possible_our,
                "possibleErrorsOpp": possible_opp,
            }.items()
        },
    }
    return quality, unique_decisions, analyzed_moves


def find_trick_end(events: list[dict], hand_id: Any, trick_no: Any) -> dict | None:
    for e in events:
        if e.get("type") != "trick_end":
            continue
        if str(e.get("handId")) != str(hand_id):
            continue
        if trick_no is not None and e.get("trickNo") is not None and e.get("trickNo") != trick_no:
            continue
        if trick_no is None or e.get("trickNo") == trick_no:
            return e
    # fallback: any trick_end same hand after plays — last matching without trickNo filter if unique
    cands = [e for e in events if e.get("type") == "trick_end" and str(e.get("handId")) == str(hand_id)]
    if trick_no is not None:
        cands = [e for e in cands if e.get("trickNo") == trick_no]
    return cands[0] if len(cands) == 1 else (cands[-1] if cands else None)


def find_decision_for_move(move: dict, decisions: list[dict], events: list[dict]) -> dict:
    """Связать ход анализатора с decision: по decisionId, иначе handId+seat+card."""
    did = move.get("decisionId")
    if did:
        hit = next((d for d in decisions if d.get("decisionId") == did), None)
        if hit:
            return hit
    seat = move.get("seat")
    hand_id = move.get("handId")
    card = move.get("card")
    for d in decisions:
        if str(d.get("handId")) != str(hand_id):
            continue
        if d.get("seat") != seat:
            continue
        action = first_defined(d.get("confirmedAction"), d.get("chosenAction"))
        if card is not None and action == card:
            return d
    # fallback: raw decision event with state
    for e in events:
        if e.get("type") != "decision":
            continue
        if str(e.get("handId")) != str(hand_id) or e.get("seat") != seat:
            continue
        action = first_defined(e.get("confirmedAction"), e.get("chosenAction"))
        if card is not None and action != card:
            continue
        if isinstance(e.get("state"), dict):
            return {
                "decisionId": e.get("decisionId"),
                "handId": e.get("handId"),
                "seat": e.get("seat"),
                "phase": e.get("phase"),
                "state": e.get("state"),
                "legalActions": e.get("legalActions"),
                "ruleId": e.get("ruleId"),
                "reason": e.get("reason"),
                "chosenAction": e.get("chosenAction"),
                "confirmedAction": e.get("confirmedAction"),
                "sourceLines": [e.get("_srcLine")],
                "sourceFile": e.get("_srcFile"),
            }
    return {}


def build_sample(move: dict, journals_by_gid: dict, label: str, decisions_by_gid: dict | None = None) -> dict:
    gid = move["gameId"]
    j = journals_by_gid[gid]
    events = j["events"]
    decisions = (decisions_by_gid or {}).get(gid) or []
    dec = move.get("_decision") or find_decision_for_move(move, decisions, events)
    state = dec.get("state") if isinstance(dec.get("state"), dict) else {}
    seat = first_defined(move.get("seat"), dec.get("seat"))
    hand_id = first_defined(move.get("handId"), dec.get("handId"))
    trick_no = move.get("trickNo")  # may be 0 — не через `or`

    # state_at: только то, что известно до решения (не подмешивать будущее)
    sat = None
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from belotmd.history.state_at import build_state_at_decision
        if dec:
            sat = build_state_at_decision(decision=dec, events=events)
    except Exception:
        sat = None

    if sat and isinstance(sat.get("hand"), dict) and sat["hand"].get("cards") is not None:
        hand_info = sat["hand"]
    else:
        hand_info = seat_hand_info(state, seat)

    if sat and isinstance(sat.get("trump"), dict) and not sat["trump"].get("unknown"):
        trump_info = dict(sat["trump"])
        trump_info["trumpLabel"] = {0: "♦", 1: "♥", 2: "♣", 3: "♠"}.get(trump_info.get("trump"))
    else:
        trump_info = trump_from_decision_or_events(dec, events) if dec else {
            "trump": None, "unknown": True, "source": None, "declarer": None, "trumpOpen": None, "trumpLabel": None
        }
        if trump_info.get("unknown"):
            # после игры: только для отображения, marked recoveredAfterGame
            for e in events:
                if str(e.get("handId")) != str(hand_id):
                    continue
                if e.get("type") in ("trump", "hand_start") and e.get("trump") is not None:
                    trump_info = {
                        "trump": e.get("trump"),
                        "trumpLabel": {0: "♦", 1: "♥", 2: "♣", 3: "♠"}.get(e.get("trump")),
                        "declarer": e.get("declarer"),
                        "trumpOpen": e.get("trumpOpen"),
                        "source": f"event:{e.get('type')} line={e.get('_srcLine')} (recoveredAfterGame)",
                        "unknown": False,
                        "knownAtDecision": False,
                    }
                    break

    table = move.get("tableBefore")
    if table is None and sat is not None:
        table = sat.get("table")
    if table is None:
        table = []
    previously = None
    if sat and isinstance(sat.get("graveyard"), list):
        previously = sat.get("graveyard")
    elif isinstance(state.get("graveyard"), list):
        previously = state.get("graveyard")
    else:
        previously = state.get("tricks_played")
    trick_end = find_trick_end(events, hand_id, trick_no)
    # full trick cards: tableBefore + this card + maybe from trick_end
    trick_cards = list(table) if isinstance(table, list) else []
    card = move.get("card")
    trick_cards = trick_cards + [{"seat": seat, "card": card}]
    if trick_end and isinstance(trick_end.get("cards"), list):
        final_cards = trick_end.get("cards")
    else:
        final_cards = trick_cards

    post = move.get("evalPostReveal") if isinstance(move.get("evalPostReveal"), dict) else {}
    at = move.get("evalAtTime") if isinstance(move.get("evalAtTime"), dict) else {}

    # void suit: lead suit present on table, not in hand cards
    void = False
    void_note = None
    if isinstance(table, list) and table and isinstance(hand_info.get("cards"), list):
        lead = table[0].get("card") if isinstance(table[0], dict) else None
        if isinstance(lead, int):
            lead_suit = lead // 8
            if not any(int(c) // 8 == lead_suit for c in hand_info["cards"] if isinstance(c, int)):
                void = True
                void_note = f"нет масти взятки {SUITS[lead_suit]}"

    return {
        "label": label,
        "gameId": gid,
        "matchKey": j.get("matchKey"),
        "handId": hand_id,
        "trickNo": trick_no,
        "decisionId": move.get("decisionId"),
        "bot": {
            "login": j.get("login"),
            "agent": j.get("agent"),
            "agentVersion": j.get("agentVersion"),
            "seat": seat,
        },
        "trump": {
            "id": trump_info.get("trump"),
            "label": trump_info.get("trumpLabel"),
            "declarer": trump_info.get("declarer"),
            "trumpOpen": trump_info.get("trumpOpen"),
            "source": trump_info.get("source"),
            "knownAtDecision": not trump_info.get("unknown"),
        },
        "handBefore": {
            "cards": [card_pair(c) for c in (hand_info.get("cards") or [])],
            "cardIds": hand_info.get("cards"),
            "known": hand_info.get("known"),
            "complete": hand_info.get("complete"),
            "count": hand_info.get("count"),
            "source": hand_info.get("source"),
        },
        "tableBefore": {
            "plays": [
                {"seat": p.get("seat"), "card": card_pair(p.get("card"))}
                for p in (table or [])
                if isinstance(p, dict)
            ],
            "source": "analyze.tableBefore",
        },
        "previouslyPlayed": previously,
        "legalActions": {
            "ids": move.get("legalActions") or dec.get("legalActions"),
            "labels": [card_label(c) for c in (move.get("legalActions") or dec.get("legalActions") or [])],
            "source": "analyze.legalActions|decision.legalActions",
        },
        "chosen": {
            "card": card_pair(card),
            "ruleId": first_defined(move.get("ruleId"), dec.get("ruleId")),
            "reason": first_defined(move.get("reason"), dec.get("reason")),
            "source": "analyze.card + decision",
        },
        "isLast": move.get("isLast"),
        "voidSuit": void,
        "voidNote": void_note,
        "trickResult": {
            "cards": [
                {"seat": p.get("seat") if isinstance(p, dict) else None,
                 "card": card_pair(p.get("card") if isinstance(p, dict) else p)}
                for p in (final_cards or [])
            ],
            "winner": first_defined(move.get("winner"), (trick_end or {}).get("winner")),
            "points": (trick_end or {}).get("points"),
            "source": f"trick_end line={(trick_end or {}).get('_srcLine')}" if trick_end else "analyze.winner only",
        },
        "evalAtTime": at,
        "evalPostReveal": post,
        "knownToBotAtChoice": {
            "hand": hand_info.get("cards"),
            "tableBefore": table,
            "trump": trump_info.get("trump"),
            "legalActions": move.get("legalActions") or dec.get("legalActions"),
        },
        "recoveredAfterGame": {
            "winner": move.get("winner"),
            "evalPostReveal": post.get("label"),
            "fullTrick": final_cards,
        },
        "sources": {
            "journalFile": j["file"],
            "decisionLines": dec.get("sourceLines"),
            "analyzeKind": move.get("kind"),
            "reconstructionStatus": move.get("reconstructionStatus"),
        },
        "ownerMark": None,
        "ownerPreferredCard": None,
        "ownerNote": None,
    }


def pick_samples(
    moves: list[dict],
    journals: list[dict],
    decisions: list[dict] | None = None,
    n: int = 10,
) -> tuple[list[dict], list[str]]:
    by_gid = {j["gameId"]: j for j in journals}
    decisions_by_gid: dict[str, list[dict]] = defaultdict(list)
    for d in decisions or []:
        decisions_by_gid[d["gameId"]].append(d)
    card_plays = [m for m in moves if m.get("kind") == "card_play"]
    missing_cats: list[str] = []
    picked: list[dict] = []
    used: set[int] = set()

    def usable(sample: dict) -> bool:
        if sample.get("chosen", {}).get("card") is None:
            return False
        # для ручного разбора нужна рука бота или хотя бы legal+trump
        has_hand = bool(sample.get("handBefore", {}).get("cardIds"))
        has_legal = bool(sample.get("legalActions", {}).get("ids"))
        has_trump = sample.get("trump", {}).get("id") is not None
        return has_hand or (has_legal and has_trump)

    def want(pred, label: str, prefer_us: bool = True) -> bool:
        # сначала наши слинкованные
        order = []
        for i, m in enumerate(card_plays):
            if i in used or not pred(m):
                continue
            order.append((0 if (m.get("us") and m.get("linkedDecision")) else 1 if m.get("us") else 2, i, m))
        if not prefer_us:
            order = [(0, i, m) for i, m in enumerate(card_plays) if i not in used and pred(m)]
        order.sort(key=lambda t: t[0])
        for _, i, m in order:
            sample = build_sample(m, by_gid, label, decisions_by_gid)
            if not usable(sample):
                continue
            used.add(i)
            picked.append(sample)
            return True
        missing_cats.append(label)
        return False

    want(lambda m: m.get("isLast") is True and m.get("us"), "last_trick_play")
    want(lambda m: m.get("isLast") is False and m.get("us"), "mid_trick_play")
    def is_trump_play(m: dict) -> bool:
        if not m.get("us") or not isinstance(m.get("card"), int):
            return False
        st = ((m.get("_decision") or {}).get("state") or {})
        trump = st.get("trump")
        if not isinstance(trump, int):
            # recover from journal events later in build_sample; accept linked plays
            return bool(m.get("linkedDecision"))
        return (m.get("card") // 8) == trump

    want(is_trump_play, "trump_play")
    want(
        lambda m: m.get("us")
        and isinstance(m.get("tableBefore"), list)
        and len(m.get("tableBefore") or []) > 0,
        "follow_or_void_candidate",
    )
    want(
        lambda m: (m.get("evalPostReveal") or {}).get("category") == "возможная_ошибка" and m.get("us"),
        "possible_error_our",
    )
    want(
        lambda m: (m.get("evalPostReveal") or {}).get("category") == "спорный_ход" and m.get("us"),
        "contested_our",
    )
    want(
        lambda m: (m.get("evalPostReveal") or {}).get("category") == "подтверждённый_факт" and m.get("us"),
        "confirmed_fact_our",
    )
    # обычный козырный/некозырный без спецметки
    want(lambda m: m.get("us") and m.get("linkedDecision"), "ordinary_our")

    for i, m in enumerate(card_plays):
        if len(picked) >= n:
            break
        if i in used or not m.get("us"):
            continue
        sample = build_sample(m, by_gid, "ordinary_our", decisions_by_gid)
        if not usable(sample):
            continue
        used.add(i)
        picked.append(sample)

    for s in picked:
        if s.get("voidSuit") and s["label"] == "follow_or_void_candidate":
            s["label"] = "void_suit"

    return picked[:n], missing_cats


def samples_to_markdown(samples: list[dict]) -> str:
    lines = ["# 10 примеров для ручного разбора", ""]
    for i, s in enumerate(samples, 1):
        lines.append(f"## {i}. {s.get('label')}")
        lines.append(f"- gameId `{s.get('gameId')}` handId `{s.get('handId')}` trickNo `{s.get('trickNo')}` decisionId `{s.get('decisionId')}`")
        bot = s.get("bot") or {}
        lines.append(f"- бот {bot.get('agent')} {bot.get('agentVersion')} seat={bot.get('seat')} ({bot.get('login')})")
        tr = s.get("trump") or {}
        lines.append(f"- козырь: {tr.get('label')} (id={tr.get('id')}), open={tr.get('trumpOpen')}, source={tr.get('source')}")
        hb = s.get("handBefore") or {}
        cards = ", ".join((c or {}).get("label") or "?" for c in (hb.get("cards") or []))
        lines.append(f"- рука: [{cards}] known={hb.get('known')} complete={hb.get('complete')}")
        tb = [((p.get('card') or {}).get('label'), p.get('seat')) for p in ((s.get('tableBefore') or {}).get('plays') or [])]
        lines.append(f"- стол до хода: {tb}")
        leg = (s.get("legalActions") or {}).get("labels")
        lines.append(f"- legal: {leg}")
        ch = ((s.get("chosen") or {}).get("card") or {})
        lines.append(f"- выбрано: {ch.get('label')} rule={((s.get('chosen') or {}).get('ruleId'))} — {(s.get('chosen') or {}).get('reason')}")
        lines.append(f"- isLast={s.get('isLast')} void={s.get('voidSuit')} {s.get('voidNote') or ''}")
        trr = s.get("trickResult") or {}
        lines.append(f"- итог взятки: winner={trr.get('winner')} points={trr.get('points')} cards={trr.get('cards')}")
        at = s.get("evalAtTime") or {}
        post = s.get("evalPostReveal") or {}
        lines.append(f"- evalAtTime: {at.get('label')} / {at.get('category')} — {at.get('explanation')}")
        lines.append(f"- evalPostReveal: {post.get('label')} / {post.get('category')} — {post.get('explanation')}")
        lines.append(f"- sources: {s.get('sources')}")
        lines.append(f"- ownerMark={s.get('ownerMark')} ownerPreferredCard={s.get('ownerPreferredCard')} ownerNote={s.get('ownerNote')}")
        lines.append("")
    return "\n".join(lines)


def write_quality_md(quality: dict, journals: list[dict], pack_name: str) -> str:
    ctrl = quality.get("controlCheck") or {}
    ctrl_lines = "\n".join(
        f"| {k} | {v['expected']} | {v['actual']} | {'OK' if v['ok'] else 'FAIL'} |"
        for k, v in ctrl.items()
    )
    return f"""# Отчёт о качестве данных (recorder 2.1) — v2

Собрано: {datetime.now().isoformat(timespec="seconds")}
Пакет: `{pack_name}`
Снимок: одна неизменяемая копия {len(journals)} журналов.

## Контроль согласованности

| Показатель | Ожидалось | Факт | |
|------------|-----------|------|--|
{ctrl_lines}

## Сводка

- Уникальных matchKey: **{quality['uniqueMatches']}**
- Участий (журналов): **{quality['participations']}**
- Записи decision: **{quality['decisionEventRecords']}**; уникальные решения: **{quality['uniqueDecisions']}**
- Игровые / подтверждённые / отклонённые / без подтверждения: **{quality['playDecisions']}** / **{quality['playConfirmed']}** / **{quality['playRejected']}** / **{quality['playUnconfirmed']}**
- Торговля: **{quality['bidDecisions']}** (confirmed {quality['bidConfirmed']}, rejected {quality['bidRejected']}, unconfirmed {quality['bidUnconfirmed']}) — {quality['noteBidUnconfirmed']}
- Сыгранные карты (play): **{quality['plays']}**
- hand_end событий / уникальных раздач: **{quality['handEndEvents']}** / **{quality['uniqueHandsWithEnd']}**
- complete / partial: **{quality['completeHands']}** / **{quality['partialHands']}**
- partial по числу взяток: `{json.dumps(quality.get('partialTrickHistogram'), ensure_ascii=False)}`
- unknown trump в уникальных игровых решениях: **{quality['unknownTrumpUniquePlayDecisions']}** (ожид. 0)
- incomplete own hand (play decisions): **{quality['incompleteOwnHandPlayDecisions']}**
- legalTrusted true/false/unknown: **{quality['legalTrustedTrue']}** / **{quality['legalTrustedFalse']}** / **{quality['legalTrustedUnknown']}**
- возможные ошибки our/opp (evalPostReveal): **{quality['possibleErrorsOur']}** / **{quality['possibleErrorsOpp']}** — {quality['possibleErrorsNote']}
- доля reliable / contested / insufficient: {quality['shareReliable']} / {quality['shareContested']} / {quality['shareInsufficient']}

## Partial детали

```json
{json.dumps(quality.get('partialDetails'), ensure_ascii=False, indent=2)}
```

## Категории evalPostReveal

our: `{json.dumps(quality.get('evalPostRevealCategoriesOur'), ensure_ascii=False)}`  
opp: `{json.dumps(quality.get('evalPostRevealCategoriesOpp'), ensure_ascii=False)}`

## Parse errors

{json.dumps(quality.get('parseErrors'), ensure_ascii=False, indent=2)}
"""


def locate_pack() -> Path | None:
    packs = HIST / "packs"
    cand = packs / "опыт_27.10.2026_002.json"
    if cand.is_file():
        return cand
    files = sorted(packs.glob("опыт_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def zip_code(dst: Path) -> list[str]:
    skipped = []
    if dst.exists():
        dst.unlink()
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.endswith(".egg-info")]
            rel_dir = Path(dirpath).relative_to(ROOT)
            parts = rel_dir.parts
            if parts[:2] == ("desk", "history"):
                # keep only lightweight review outputs, not bots/packs dumps
                if len(parts) >= 3 and parts[2] in {
                    "bots", "jobs", "packs", "backup_2026-10-27_tz", "backup_2026-10-27_review2",
                    "recalc_rules2", "review_pack_staging", "review_freeze_v21", "index",
                }:
                    dirnames[:] = []
                    continue
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(ROOT).as_posix()
                if SECRET_NAME_RE.search(name) or SECRET_NAME_RE.search(rel):
                    skipped.append(rel)
                    continue
                if name.endswith((".pyc", ".pyo", ".pyd")):
                    continue
                if path.stat().st_size > 5_000_000 and "packs" in rel:
                    skipped.append(rel)
                    continue
                zf.write(path, f"belotmd_agent/{rel}")
        zf.writestr(
            "INSTALL_AND_TEST.md",
            "# Проверка\n\n```\ncd belotmd_agent\npython -m pip install -e .[dev]\n"
            "pytest -q\npython tools/collect_review_pack_v21.py\n```\n",
        )
    return skipped


def zip_data(
    dst: Path,
    journals: list[dict],
    pack_path: Path | None,
    quality: dict,
    samples: list[dict],
    quality_md: str,
    samples_md: str,
    comparison_md: str = "",
) -> None:
    if dst.exists():
        dst.unlink()
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for j in journals:
            zf.write(j["path"], f"journals/{j['file']}")
        if pack_path and pack_path.is_file():
            zf.write(pack_path, f"packs/{pack_path.name}")
        q = {k: v for k, v in quality.items()}
        zf.writestr("quality_report.json", json.dumps(q, ensure_ascii=False, indent=2, default=str))
        zf.writestr("QUALITY_REPORT.md", quality_md)
        if comparison_md:
            zf.writestr("COMPARISON.md", comparison_md)
        zf.writestr("manual_review_samples.json", json.dumps(samples, ensure_ascii=False, indent=2, default=str))
        zf.writestr("manual_review_samples.md", samples_md)
        fixes = ROOT / "docs" / "independent_review_fixes_table.md"
        if fixes.is_file():
            zf.write(fixes, "independent_review_fixes_table.md")
        zf.writestr(
            "journals_index.json",
            json.dumps(
                [{k: v for k, v in j.items() if k not in ("events", "path", "snapshotDir")} for j in journals],
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
        )


def update_fixes_table() -> None:
    path = ROOT / "docs" / "independent_review_fixes_table.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else "# Замечания независимой проверки → статус\n"
    # replace stale «открытые» блок статусом v3
    v3 = """

## Закрыто в v3 (rules-2.2 / reconstruct 2.2 / pack) — 2026-10-27

| # | Замечание | Статус | Где / тест |
|---|-----------|--------|------------|
| 1 | `trump=None` trusted / «возможная ошибка» | **исправлено** | `analyze.py` `trump_unknown`; NT без флага остаётся NT |
| 2 | `evaluatedShare` без спорных / insufficient | **исправлено** | доли `shareReliable/Contested/Insufficient` в отчёте |
| 3 | complete при 9 взятках; повтор seat | **исправлено** | `reconstruct.py` 2.2 |
| 4 | cleanup при битой строке JSONL | **исправлено** | `history_svc.read_jsonl_strict` блокирует cleanup |
| 5 | situation keys / dedupe | **исправлено** | `keys.py` exact-3; dedupe по `gameId` |
| 6 | bidding server confirm | **исправлено** | `bot._pending_bid` / `_check_bid_accepted` |
| 7 | owner review UI | **исправлено** | `desk/static/app.js` форма ownerMark |
| — | state на момент решения | **добавлено** | `history/state_at.py` |
| — | примеры ходов / отчёт | **исправлено** | `collect_review_pack_v21.py` → `belot_review_*_v3` |

Сравнение цифр v2→v3: `COMPARISON.md` в data-архиве. Контроль структуры журналов (1007/533/474/…) сохранён.
"""
    if "Закрыто в v3" not in text:
        # mark old open rows as superseded
        text = text.replace("## Открытые после повторной проверки архива", "## Открытые после повторной проверки (superseded → v3)")
        path.write_text(text.rstrip() + v3, encoding="utf-8")


def write_comparison_md(quality: dict) -> str:
    delta = quality.get("qualityDeltaVsV2") or {}
    def row(key: str) -> str:
        d = delta.get(key) or {}
        return f"| {key} | {d.get('v2')} | {d.get('v3')} | {'да' if d.get('changed') else 'нет'} |"

    return f"""# Сравнение анализа: rules-2.1 (v2 отчёт) → rules-2.2 / reconstruct 2.2

Структура журналов (decision/hand_end/plays) не менялась — контроль `controlCheck` = {all(v.get('ok') for v in (quality.get('controlCheck') or {}).values())}.

| Показатель | v2 | v3 | изменилось |
|------------|----|----|------------|
{row('completeHands')}
{row('partialHands')}
{row('possibleErrorsOur')}
{row('possibleErrorsOpp')}
| unknownTrumpUniquePlayDecisions | 0 | {quality.get('unknownTrumpUniquePlayDecisions')} | — |
| legalTrustedUnknown | — | {quality.get('legalTrustedUnknown')} | — |
| shareReliable pct | — | {(quality.get('shareReliable') or {}).get('pct')} | — |
| shareContested pct | — | {(quality.get('shareContested') or {}).get('pct')} | — |
| shareInsufficient pct | — | {(quality.get('shareInsufficient') or {}).get('pct')} | — |

STRONG ≠ Fallen; `trump_unknown` ≠ NT; отсутствие `legalTrusted` ≠ true.
"""


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Belot review pack collector v3")
    ap.add_argument("--input-data", type=Path, default=None, help="path to belot_review_data_*.zip")
    ap.add_argument("--output-dir", type=Path, default=OUT, help="where to write v3 archives")
    ap.add_argument("--pack", type=Path, default=None, help="optional pack json to embed")
    args = ap.parse_args(argv)

    out_dir = Path(args.output_dir)
    code_zip = out_dir / "belot_review_code_v3.zip"
    data_zip = out_dir / "belot_review_data_v3.zip"
    manifest_path = out_dir / "belot_review_MANIFEST_v3.json"

    sources = find_source_journals(args.input_data)
    if len(sources) < 5:
        print(f"NEED_5_JOURNALS got={len(sources)}", flush=True)
        return 2
    journals = freeze_journals(sources[:5])
    print(
        "frozen",
        [{k: j[k] for k in ("file", "gameId", "sha256", "eventCount", "recorderVersion")} for j in journals],
        flush=True,
    )

    quality, decisions, moves = analyze_journals(journals)
    samples, missing = pick_samples(moves, journals, decisions, 10)
    quality["sampleMissingCategories"] = missing
    pack_path = Path(args.pack) if args.pack else locate_pack()
    pack_name = pack_path.name if pack_path and pack_path.is_file() else ""
    quality_md = write_quality_md(quality, journals, pack_name)
    samples_md = samples_to_markdown(samples)
    comparison_md = write_comparison_md(quality)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (REPORT_DIR / "QUALITY_REPORT.md").write_text(quality_md, encoding="utf-8")
    (REPORT_DIR / "COMPARISON.md").write_text(comparison_md, encoding="utf-8")
    (REPORT_DIR / "manual_review_samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (REPORT_DIR / "manual_review_samples.md").write_text(samples_md, encoding="utf-8")

    update_fixes_table()
    skipped = zip_code(code_zip)
    zip_data(
        data_zip,
        journals,
        pack_path if pack_path and pack_path.is_file() else None,
        quality,
        samples,
        quality_md,
        samples_md,
        comparison_md,
    )

    ctrl_ok = all(v.get("ok") for v in (quality.get("controlCheck") or {}).values())
    manifest = {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "purpose": "v3: history/analyzer/pack fixes + report from frozen recorder 2.1 journals",
        "analyzer": "rules-2.2",
        "reconstruct": "2.2",
        "pack": pack_name,
        "journals": [
            {k: j[k] for k in (
                "file", "gameId", "botId", "login", "agent", "agentVersion",
                "recorderVersion", "schemaVersion", "buildId", "bytes", "sha256",
                "eventCount", "tMin", "tMax",
            )}
            for j in journals
        ],
        "controlCheckOk": ctrl_ok,
        "controlCheck": quality.get("controlCheck"),
        "qualitySummary": {
            "completeHands": quality.get("completeHands"),
            "partialHands": quality.get("partialHands"),
            "possibleErrorsOur": quality.get("possibleErrorsOur"),
            "possibleErrorsOpp": quality.get("possibleErrorsOpp"),
            "unknownTrumpUniquePlayDecisions": quality.get("unknownTrumpUniquePlayDecisions"),
        },
        "samples": len(samples),
        "sampleMissingCategories": missing,
        "archives": {
            code_zip.name: {"sha256": sha256_file(code_zip), "bytes": code_zip.stat().st_size},
            data_zip.name: {"sha256": sha256_file(data_zip), "bytes": data_zip.stat().st_size},
        },
        "skippedSecrets": skipped[:80],
        "AUTO_CLEANUP": "0",
        "notes": [
            "Immutable freeze snapshot",
            "Strategies unchanged",
            "Analyzer/reconstruct/cleanup/keys/bidding-confirm/UI marks updated for v3 TZ",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    desk = Path.home() / "Desktop" / "belot_review_v3"
    desk.mkdir(parents=True, exist_ok=True)
    for p in (code_zip, data_zip, manifest_path):
        shutil.copy2(p, desk / p.name)

    print(json.dumps({
        "ok": ctrl_ok,
        "codeZip": str(code_zip),
        "dataZip": str(data_zip),
        "manifest": str(manifest_path),
        "desktop": str(desk),
        "sha256": manifest["archives"],
        "controlCheck": quality.get("controlCheck"),
        "qualitySummary": manifest["qualitySummary"],
        "samples": len(samples),
        "pack": pack_name,
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if ctrl_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
