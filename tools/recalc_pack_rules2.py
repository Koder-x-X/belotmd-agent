"""Пересчёт ночного пакета анализатором rules-2 (ТЗ §10).

Не перезаписывает исходный файл. Пишет новую сборку + отчёт + 20–30
примеров для ручного разбора.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from belotmd.history.analyze import ANALYZER_VERSION, analyze_game
from belotmd.history.reconstruct import reconstruct_game

PACKS = ROOT / "desk" / "history" / "packs"
SRC_NAME = "опыт_27.10.2026_001.json"
OUT_DIR = ROOT / "desk" / "history" / "recalc_rules2"
REVIEW_N = 28


def _next_name(day: str) -> tuple[str, int]:
    seq = 1
    while True:
        name = f"опыт_{day}_{seq:03d}_rules2.json"
        if not (PACKS / name).exists() and not (OUT_DIR / name).exists():
            return name, seq
        seq += 1


def main() -> int:
    src = PACKS / SRC_NAME
    if not src.is_file():
        print(f"missing {src}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"loading {src} ...")
    pack = json.loads(src.read_text(encoding="utf-8"))
    games = pack.get("games") or []
    old_decisions = {
        (d.get("gameId"), d.get("decisionId")): d
        for d in (pack.get("decisions") or [])
        if isinstance(d, dict)
    }

    day = datetime.now().strftime("%d.%m.%Y")
    out_name, seq = _next_name(day)
    pack_id = f"pack_rules2_{day.replace('.', '')}_{seq:03d}"

    new_games = []
    reconstructed_all = []
    decisions_all = []
    quality = {
        "sourcePack": SRC_NAME,
        "sourcePackId": pack.get("packId"),
        "analyzer": ANALYZER_VERSION,
        "incomplete": [],
        "excluded": [],
        "warnings": [],
        "reconstruction": [],
        "labelChanges": 0,
        "analysisLimits": [
            "rules-2: сила/очки из strong_agent",
            "альтернативы только из legal / follow-suit",
            "mid-trick не даёт авто-ошибку «мог забрать»",
            "post_reveal ≠ обязанность игрока на момент хода",
        ],
    }
    agent_stats = defaultdict(lambda: {
        "games": 0,
        "ourCardMoves": 0,
        "ourMovesEvaluated": 0,
        "oppCardMoves": 0,
        "biddingMoves": 0,
        "unevalOur": 0,
        "labelsPostOur": Counter(),
        "labelsPostOpp": Counter(),
        "possibleErrorsOur": 0,
        "possibleErrorsOpp": 0,
        "candidatesOur": 0,
        "candidatesOpp": 0,
        # legacy aliases for report compatibility
        "ourMoves": 0,
        "possibleErrors": 0,
        "candidates": 0,
        "labelsPost": Counter(),
        "labelsAt": Counter(),
    })
    recon_counts = Counter()
    unique_matches = set()
    bot_participations = 0
    confirmed_moves = 0
    hands_total = 0

    for i, g in enumerate(games):
        if i and i % 25 == 0:
            print(f"  ... {i}/{len(games)}")
        gid = g.get("gameId")
        events = g.get("events") or []
        meta = g.get("meta") or {}
        agent = str(meta.get("agent") or "unknown")
        mk = meta.get("matchKey")
        if mk:
            unique_matches.add(str(mk))
        bot_participations += 1
        agent_stats[agent]["games"] += 1

        try:
            recon = reconstruct_game(events)
            decisions = analyze_game(events, recon)
        except Exception as exc:
            quality["warnings"].append({"gameId": gid, "error": str(exc)})
            recon, decisions = [], []

        for r in recon:
            hands_total += 1
            recon_counts[r.get("status") or "?"] += 1
            if r.get("status") != "complete":
                quality["reconstruction"].append({
                    "gameId": gid,
                    "handId": r.get("handId"),
                    "status": r.get("status"),
                })
            reconstructed_all.append({"gameId": gid, **r})

        for d in decisions:
            d_out = {"gameId": gid, **d}
            decisions_all.append(d_out)
            post = d.get("evalPostReveal") or {}
            at = d.get("evalAtTime") or {}
            cat = post.get("category")
            st = agent_stats[agent]
            if d.get("kind") == "agent_decision":
                st["biddingMoves"] += 1
            elif d.get("card") is not None:
                if d.get("us"):
                    confirmed_moves += 1
                    st["ourCardMoves"] += 1
                    st["ourMoves"] += 1
                    if post.get("label") and post.get("label") != "неоценённый":
                        st["ourMovesEvaluated"] += 1
                    else:
                        st["unevalOur"] += 1
                    if post.get("label"):
                        st["labelsPostOur"][post["label"]] += 1
                        st["labelsPost"][post["label"]] += 1
                    if at.get("label"):
                        st["labelsAt"][at["label"]] += 1
                    if cat == "возможная_ошибка":
                        st["possibleErrorsOur"] += 1
                        st["possibleErrors"] += 1
                    if cat == "кандидат_на_разбор":
                        st["candidatesOur"] += 1
                        st["candidates"] += 1
                else:
                    st["oppCardMoves"] += 1
                    if post.get("label"):
                        st["labelsPostOpp"][post["label"]] += 1
                    if cat == "возможная_ошибка":
                        st["possibleErrorsOpp"] += 1
                    if cat == "кандидат_на_разбор":
                        st["candidatesOpp"] += 1

            old = old_decisions.get((gid, d.get("decisionId")))
            if old:
                old_lab = (old.get("evalPostReveal") or {}).get("label")
                new_lab = post.get("label")
                if old_lab and new_lab and old_lab != new_lab:
                    quality["labelChanges"] += 1

        new_games.append({
            "gameId": gid,
            "meta": {**meta, "reconstruction": {
                "analyzer": ANALYZER_VERSION,
                "handsComplete": sum(1 for r in recon if r.get("status") == "complete"),
                "handsPartial": sum(1 for r in recon if r.get("status") == "partial"),
                "handsInvalid": sum(1 for r in recon if r.get("status") == "invalid"),
            }},
            "events": events,
        })

    # 20–30 review samples: diverse possible errors / candidates / our moves
    review = []
    seen_keys = set()
    priority = ("возможная_ошибка", "кандидат_на_разбор", "спорный_ход")
    for want in priority:
        for d in decisions_all:
            if len(review) >= REVIEW_N:
                break
            post = d.get("evalPostReveal") or {}
            if post.get("category") != want and post.get("label") != want:
                continue
            if not d.get("us"):
                continue
            key = (d.get("gameId"), d.get("decisionId"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            review.append({
                "gameId": d.get("gameId"),
                "decisionId": d.get("decisionId"),
                "handId": d.get("handId"),
                "trickNo": d.get("trickNo"),
                "seat": d.get("seat"),
                "card": d.get("card"),
                "cardToken": d.get("cardToken"),
                "tableBefore": d.get("tableBefore"),
                "isLast": d.get("isLast"),
                "winner": d.get("winner"),
                "reconstructionStatus": d.get("reconstructionStatus"),
                "evalAtTime": d.get("evalAtTime"),
                "evalPostReveal": d.get("evalPostReveal"),
                "ownerMark": None,
                "ownerPreferredCard": None,
                "ownerNote": None,
                "reviewPrompt": {
                    "mark": ["правильно", "ошибка", "спорно"],
                    "hint": "отметить желаемую карту и объяснение отдельно от авто-оценки",
                },
            })
        if len(review) >= REVIEW_N:
            break

    created = time.time()
    out_pack = {
        "format": "2",
        "handler": "belotmd-experience-2",
        "packId": pack_id,
        "filename": out_name,
        "createdAt": created,
        "timezone": pack.get("timezone") or "Europe/Chisinau",
        "day": day,
        "daySeq": seq,
        "recalculatedFrom": {
            "filename": SRC_NAME,
            "packId": pack.get("packId"),
            "analyzerWas": (pack.get("quality_report") or {}).get("analyzer"),
            "analyzerNow": ANALYZER_VERSION,
        },
        "metadata": {
            **(pack.get("metadata") or {}),
            "recalc": True,
            "analyzer": ANALYZER_VERSION,
            "sourceGames": len(games),
        },
        "games": new_games,
        "reconstructed_hands": reconstructed_all,
        "decisions": decisions_all,
        "situations": [],  # ключи пересобираются при новой desk-сборке
        "quality_report": quality,
        "checksum": None,
    }
    raw = json.dumps(out_pack, ensure_ascii=False, default=str).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    out_pack["checksum"] = digest

    out_path = OUT_DIR / out_name
    out_path.write_text(
        json.dumps(out_pack, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # копия в packs без удаления исходника
    packs_copy = PACKS / out_name
    packs_copy.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")

    report = {
        "generatedAt": created,
        "source": SRC_NAME,
        "output": str(out_path),
        "packsCopy": str(packs_copy),
        "packId": pack_id,
        "analyzer": ANALYZER_VERSION,
        "games": len(new_games),
        "uniqueMatches": len(unique_matches),
        "botParticipations": bot_participations,
        "noteUnits": (
            "155 игр в пакете = участия ботов в матчах; "
            f"уникальных matchKey={len(unique_matches)}; "
            f"participations={bot_participations}. "
            "Сравнение 74+96=170 могло считать два бота в одних матчах отдельно "
            "и/или смешивать периоды — здесь единицы разделены явно."
        ),
        "hands": dict(recon_counts),
        "handsTotal": hands_total,
        "confirmedOurMoves": confirmed_moves,
        "decisionsTotal": len(decisions_all),
        "labelChangesFromOldPack": quality["labelChanges"],
        "excluded": quality["excluded"],
        "warnings": quality["warnings"][:50],
        "agentStats": {
            a: {
                **{
                    k: v for k, v in st.items()
                    if k not in (
                        "labelsPost", "labelsAt", "labelsPostOur", "labelsPostOpp",
                    )
                },
                "labelsPost": dict(st["labelsPost"]),
                "labelsAt": dict(st["labelsAt"]),
                "labelsPostOur": dict(st["labelsPostOur"]),
                "labelsPostOpp": dict(st["labelsPostOpp"]),
                "evaluatedShareOur": (
                    round(100.0 * st["ourMovesEvaluated"] / st["ourCardMoves"], 1)
                    if st["ourCardMoves"] else None
                ),
                "possibleErrorRateOur": (
                    round(100.0 * st["possibleErrorsOur"] / st["ourCardMoves"], 1)
                    if st["ourCardMoves"] else None
                ),
                "controlNote": (
                    "Контроль фильтра us на старом rules-2 файле (до 2.1): "
                    "Fallen our=6477 err≈464; Fallen2 our=8768 err≈617 — "
                    "не цель для подгонки новых оценок."
                ),
            }
            for a, st in agent_stats.items()
        },
        "decisionBreakdown": {
            "total": len(decisions_all),
            "ourCard": sum(1 for d in decisions_all if d.get("us") and d.get("card") is not None),
            "oppCard": sum(1 for d in decisions_all if (not d.get("us")) and d.get("card") is not None),
            "bidding": sum(1 for d in decisions_all if d.get("kind") == "agent_decision"),
        },
        "reviewSamples": len(review),
    }
    report_path = OUT_DIR / "recalc_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    review_path = OUT_DIR / "manual_review_samples.json"
    review_path.write_text(
        json.dumps({
            "instruction": (
                "Отметьте ownerMark: правильно|ошибка|спорно; "
                "ownerPreferredCard; ownerNote. "
                "Не смешивать с авто-оценками. Принятые правила → отдельные тесты."
            ),
            "samples": review,
        }, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # comparison setup doc (no auto-start)
    cmp_path = OUT_DIR / "comparison_setup.md"
    cmp_path.write_text(
        f"""# Сравнение Fallen / Fallen2 / Fallen21 (без автозапуска)

Подготовлено после пересчёта `{out_name}`.

## Версии
- **fallen** — основная стратегия
- **fallen2** — v2.2, EXP-Take/Cheap только последним ходом
- **fallen21** — v2.1, mid-trick EXP (контроль)

## Условия
- Одинаковый режим (3p/4p) и настройки стола
- Один период наблюдения
- Фиксированные версии стратегий в профилях desk
- Чередование стратегий между аккаунтами или блоками времени
- Отдельно: незавершённые игры; партии с несколькими нашими ботами

## Метрики
Основные: доля побед, результат раздач, очки.
Вспомогательные: метки analyzer rules-2 (не доказательство силы стратегии).

## Объём
Заранее зафиксировать длительность или N игр. При малой выборке —
«преимущество не доказано».

## Запуск
**Не выполнять автоматически.** Вручную назначить стратегии в desk/profiles
и стартовать аккаунты владельцем.
""",
        encoding="utf-8",
    )

    print(json.dumps({
        "ok": True,
        "pack": str(packs_copy),
        "report": str(report_path),
        "review": str(review_path),
        "games": len(new_games),
        "decisions": len(decisions_all),
        "labelChanges": quality["labelChanges"],
        "uniqueMatches": len(unique_matches),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
