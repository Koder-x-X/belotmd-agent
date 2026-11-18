# -*- coding: utf-8 -*-
"""Export all 'could play better' lessons from experience packs for Fallen."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PACKS = Path(__file__).resolve().parent / "history" / "packs"
OUT_DIR = Path(__file__).resolve().parent.parent / "docs"

SUIT = {
    "diamonds": "♦ бубны",
    "hearts": "♥ черви",
    "clubs": "♣ трефы",
    "spades": "♠ пики",
}
RANK = {
    "9": "9",
    "10": "10",
    "J": "валет",
    "Q": "дама",
    "K": "король",
    "A": "туз",
}
RANK_ORDER = {"9": 0, "10": 1, "J": 2, "Q": 3, "K": 4, "A": 5}

# cheaper beating card id mentioned in explanation
RE_CHEAPER = re.compile(
    r"более дешёвая бьющая карта\s+(\d+)\s*\(очки карты\s+(\d+)\s*<\s*(\d+)\)"
)


def nice(tok) -> str:
    if tok is None:
        return "?"
    s = str(tok)
    if "_" not in s:
        return s
    r, suit = s.split("_", 1)
    return f"{RANK.get(r, r)} {SUIT.get(suit, suit)}"


def tok_from_id(cid, mapping: dict) -> str:
    if cid is None:
        return "?"
    try:
        return mapping.get(int(cid), f"#{cid}")
    except (TypeError, ValueError):
        return f"#{cid}"


def rank_of(tok) -> str | None:
    if not tok or "_" not in str(tok):
        return None
    return str(tok).split("_", 1)[0]


def load_packs():
    packs = []
    skipped = []
    for path in sorted(PACKS.glob("опыт*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("decisions"):
            packs.append((path.name, data))
        else:
            skipped.append(
                {
                    "file": path.name,
                    "reason": "нет секции decisions",
                    "keys": list(data.keys()),
                    "games": len(data.get("games") or []),
                }
            )
    return packs, skipped


def classify(expl: str) -> str | None:
    if "взятка отдана" in expl and "бьющая текущий стол" in expl:
        return "missed_take"
    if "более дешёвая бьющая карта" in expl:
        return "overpay"
    return None


def rule_for_missed(played: str, suggested: str | None) -> str:
    pr = rank_of(played)
    sr = rank_of(suggested) if suggested else None
    if sr == "A" and pr in ("K", "Q", "J", "9", "10"):
        return (
            "ПРАВИЛО [не взяли]: есть туз, бьющий стол — ставим туза и забираем "
            f"(не {RANK.get(pr, pr)})"
        )
    if sr == "10" and pr in ("K", "Q", "J", "9"):
        return (
            "ПРАВИЛО [не взяли]: есть 10, бьющая стол — ставим 10 и забираем "
            f"(не {RANK.get(pr, pr)})"
        )
    if sr in ("K", "Q") and pr in ("J", "9"):
        return (
            f"ПРАВИЛО [не взяли]: есть {RANK.get(sr, sr)}, бьющая стол — "
            f"ставим её и забираем (не {RANK.get(pr, pr)})"
        )
    if sr and pr and RANK_ORDER.get(sr, 0) > RANK_ORDER.get(pr, -1):
        return (
            f"ПРАВИЛО [не взяли]: есть более сильная бьющая ({RANK.get(sr, sr)}) — "
            f"забираем, не {RANK.get(pr, pr)}"
        )
    return "ПРАВИЛО [не взяли]: среди допустимых есть карта, бьющая стол — забираем"


def rule_for_overpay(played: str, cheaper: str | None) -> str:
    pr = rank_of(played)
    cr = rank_of(cheaper) if cheaper else None
    if pr == "A" and cr:
        return (
            f"ПРАВИЛО [переплата]: взятку можно взять дешевле — "
            f"не туз, а {RANK.get(cr, cr)}"
        )
    if pr == "10" and cr:
        return (
            f"ПРАВИЛО [переплата]: взятку можно взять дешевле — "
            f"не 10, а {RANK.get(cr, cr)}"
        )
    if pr and cr:
        return (
            f"ПРАВИЛО [переплата]: вместо {RANK.get(pr, pr)} хватило бы "
            f"{RANK.get(cr, cr)}"
        )
    return "ПРАВИЛО [переплата]: взяли дорогой картой, хотя была дешевле бьющая"


def best_stronger_alt(played: str, alts: list[str]) -> str | None:
    pr = rank_of(played)
    strong = [
        a
        for a in alts
        if RANK_ORDER.get(rank_of(a) or "", -1) > RANK_ORDER.get(pr or "", -1)
    ]
    if strong:
        return max(strong, key=lambda t: RANK_ORDER.get(rank_of(t) or "", -1))
    return alts[0] if alts else None


def main() -> None:
    packs, skipped = load_packs()
    token_by_id: dict[int, str] = {}
    for _, data in packs:
        for d in data["decisions"]:
            if d.get("card") is not None and d.get("cardToken"):
                token_by_id[int(d["card"])] = d["cardToken"]

    cases_us: list[dict] = []
    cases_all: list[dict] = []
    pack_stats: list[dict] = []

    for pack_name, data in packs:
        login_by_game = {}
        for g in data.get("games") or []:
            m = g.get("meta") or {}
            gid = g.get("gameId") or m.get("gameId")
            login_by_game[gid] = m.get("login") or m.get("botId")

        meta = data.get("metadata") or {}
        n_miss_us = n_over_us = n_miss_all = n_over_all = 0
        n_dec = len(data["decisions"])

        for d in data["decisions"]:
            ep = d.get("evalPostReveal") or {}
            # also accept at_time if post empty but prefer post
            if not ep.get("explanation") and (d.get("evalAtTime") or {}).get(
                "explanation"
            ):
                ep = d.get("evalAtTime") or {}
            expl = ep.get("explanation") or ""
            kind = classify(expl)
            if not kind:
                continue

            alts_raw = ep.get("alternatives") or []
            played = d.get("cardToken") or tok_from_id(d.get("card"), token_by_id)
            alt_toks = [tok_from_id(a, token_by_id) for a in alts_raw]

            cheaper_tok = None
            m = RE_CHEAPER.search(expl)
            if m:
                cheaper_tok = tok_from_id(int(m.group(1)), token_by_id)

            if kind == "missed_take":
                suggested = best_stronger_alt(played, alt_toks)
                rule = rule_for_missed(played, suggested)
            else:
                suggested = cheaper_tok or (
                    min(
                        alt_toks,
                        key=lambda t: RANK_ORDER.get(rank_of(t) or "", 99),
                    )
                    if alt_toks
                    else None
                )
                rule = rule_for_overpay(played, suggested)

            row = {
                "kind": kind,
                "kindRu": (
                    "не взяли, хотя могли побить"
                    if kind == "missed_take"
                    else "переплата: взяли дороже, чем нужно"
                ),
                "pack": pack_name,
                "gameId": d.get("gameId"),
                "decisionId": d.get("decisionId"),
                "handId": d.get("handId"),
                "trickNo": d.get("trickNo"),
                "seat": d.get("seat"),
                "us": bool(d.get("us")),
                "login": login_by_game.get(d.get("gameId"), "?"),
                "played": played,
                "playedNice": nice(played),
                "suggested": suggested,
                "suggestedNice": nice(suggested) if suggested else "—",
                "alternatives": alt_toks,
                "alternativesNice": [nice(t) for t in alt_toks],
                "confidence": ep.get("confidence"),
                "reconstructionStatus": d.get("reconstructionStatus"),
                "explanation": expl,
                "label": ep.get("label"),
                "rule": rule,
                "evalMode": ep.get("mode") or "post_reveal",
            }
            cases_all.append(row)
            if kind == "missed_take":
                n_miss_all += 1
            else:
                n_over_all += 1
            if d.get("us"):
                cases_us.append(row)
                if kind == "missed_take":
                    n_miss_us += 1
                else:
                    n_over_us += 1

        pack_stats.append(
            {
                "file": pack_name,
                "games": meta.get("games") or len(data.get("games") or []),
                "decisions": n_dec,
                "ourMissed": n_miss_us,
                "ourOverpay": n_over_us,
                "allMissed": n_miss_all,
                "allOverpay": n_over_all,
                "ourBetter": n_miss_us + n_over_us,
                "allBetter": n_miss_all + n_over_all,
            }
        )

    # buckets by rule (our bot only — for Fallen update)
    rule_buckets: dict[str, list] = defaultdict(list)
    kind_count = Counter()
    pair_missed = Counter()
    pair_overpay = Counter()

    for c in cases_us:
        kind_count[c["kind"]] += 1
        rule_buckets[c["rule"]].append(c)
        pr = rank_of(c["played"])
        sr = rank_of(c.get("suggested"))
        if pr and sr:
            if c["kind"] == "missed_take":
                pair_missed[f"{pr} → лучше брать {sr}"] += 1
            else:
                pair_overpay[f"{pr} → дешевле {sr}"] += 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / "fallen-missed-takes-lessons.md"
    json_path = OUT_DIR / "fallen-missed-takes-lessons.json"

    sources = [n for n, _ in packs]
    n_us = len(cases_us)
    n_all = len(cases_all)

    lines: list[str] = []
    lines.append("# Уроки Fallen: где можно было сыграть лучше")
    lines.append("")
    lines.append(f"Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    lines.append(f"Источники (с decisions): {', '.join(sources)}")
    if skipped:
        lines.append(
            "Пропущены (нет decisions): "
            + ", ".join(s["file"] for s in skipped)
        )
    lines.append(
        "Метод: rules-1 (post_reveal), уверенность обычно 0.35, руки partial."
    )
    lines.append("")
    lines.append(
        "> Черновик для обновления Fallen. Не золотые эталоны "
        "(реконструкция частичная)."
    )
    lines.append(
        "> Два типа уроков: **не взяли, хотя могли побить** "
        "и **переплата** (взяли дороже, чем нужно)."
    )
    lines.append("")
    lines.append("## Сводка по пакетам")
    lines.append("")
    lines.append(
        "| Пакет | Игр | Решений | Fallen: не взяли | Fallen: переплата | "
        "Fallen всего лучше | Все игроки лучше |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for s in pack_stats:
        lines.append(
            f"| {s['file']} | {s['games']} | {s['decisions']} | "
            f"{s['ourMissed']} | {s['ourOverpay']} | {s['ourBetter']} | "
            f"{s['allBetter']} |"
        )
    tot = {
        "games": sum(s["games"] for s in pack_stats),
        "decisions": sum(s["decisions"] for s in pack_stats),
        "ourMissed": sum(s["ourMissed"] for s in pack_stats),
        "ourOverpay": sum(s["ourOverpay"] for s in pack_stats),
        "ourBetter": sum(s["ourBetter"] for s in pack_stats),
        "allBetter": sum(s["allBetter"] for s in pack_stats),
    }
    lines.append(
        f"| **ИТОГО** | {tot['games']} | {tot['decisions']} | "
        f"{tot['ourMissed']} | {tot['ourOverpay']} | **{tot['ourBetter']}** | "
        f"{tot['allBetter']} |"
    )
    lines.append("")
    lines.append("## Сводка для Fallen")
    lines.append("")
    lines.append(f"- Ходов «можно было лучше» у нашего бота: **{n_us}**")
    lines.append(
        f"  - не взяли, хотя могли побить: **{kind_count['missed_take']}**"
    )
    lines.append(
        f"  - переплата (дорогой картой при дешевле бьющей): **{kind_count['overpay']}**"
    )
    lines.append(f"- То же по всем игрокам за столом: **{n_all}**")
    lines.append("")
    lines.append("## Правила-кандидаты для Fallen")
    lines.append("")

    for key, rows in sorted(rule_buckets.items(), key=lambda kv: -len(kv[1])):
        kind_ru = rows[0]["kindRu"]
        lines.append(f"### {key}")
        lines.append(f"Тип: {kind_ru}. Наблюдений: **{len(rows)}**")
        lines.append("")
        lines.append("| Пакет | Партия | Раздача | Взятка | Сыграли | Лучше было |")
        lines.append("|---|---|---:|---:|---|---|")
        for r in rows[:15]:
            pack_short = r["pack"].replace("опыт_", "")
            gid = (r.get("gameId") or "")[:12]
            lines.append(
                f"| {pack_short} | `{gid}…` | {r['handId']} | {r['trickNo']} | "
                f"**{r['playedNice']}** | {r['suggestedNice']} |"
            )
        if len(rows) > 15:
            lines.append(f"| … | | | | ещё {len(rows) - 15} | |")
        lines.append("")

    lines.append("## Частотные пары")
    lines.append("")
    lines.append("### Не взяли: сыграли → чем лучше было брать")
    lines.append("")
    for k, v in pair_missed.most_common(25):
        lines.append(f"- `{k}` — {v} раз")
    lines.append("")
    lines.append("### Переплата: сыграли → чем дешевле хватило бы")
    lines.append("")
    for k, v in pair_overpay.most_common(25):
        lines.append(f"- `{k}` — {v} раз")
    lines.append("")
    lines.append("## Как применять в агенте")
    lines.append("")
    lines.append(
        "1. Если среди допустимых есть карта, **бьющая стол**, и цель — взять "
        "взятку: не ставить заведомо небьющую (пример: король вместо туза)."
    )
    lines.append(
        "2. Если взятку уже берём — среди бьющих предпочитать **более дешёвую** "
        "(не туз, если хватает 9/валета и т.д.)."
    )
    lines.append(
        "3. Этот файл — черновик паттернов; вслепую в обучение не лить, "
        "пока руки не `complete`."
    )
    lines.append("")
    lines.append("## Полный список (наш бот)")
    lines.append("")
    lines.append(
        "| # | Тип | Пакет | game | hand | trick | сыграли | лучше |"
    )
    lines.append("|---:|---|---|---|---:|---:|---|---|")
    for i, c in enumerate(cases_us, 1):
        pack_short = c["pack"].replace("опыт_", "")
        gid = (c.get("gameId") or "")[:14]
        tip = "не взяли" if c["kind"] == "missed_take" else "переплата"
        lines.append(
            f"| {i} | {tip} | {pack_short} | `{gid}` | {c['handId']} | "
            f"{c['trickNo']} | {c['playedNice']} | {c['suggestedNice']} |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "title": "Fallen better-play lessons",
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "sources": sources,
        "skippedPacks": skipped,
        "method": "rules-1",
        "warning": (
            "partial hands, confidence~0.35 — draft rules for Fallen update, "
            "not gold labels"
        ),
        "kinds": {
            "missed_take": "не взяли, хотя могли побить",
            "overpay": "переплата: взяли дороже бьющей",
        },
        "packStats": pack_stats,
        "totals": {
            "ourBetter": n_us,
            "ourMissedTakes": kind_count["missed_take"],
            "ourOverpay": kind_count["overpay"],
            "allBetter": n_all,
        },
        "candidateRules": [
            {
                "rule": k,
                "kind": rows[0]["kind"],
                "count": len(rows),
                "examples": [
                    {
                        "pack": r["pack"],
                        "gameId": r["gameId"],
                        "decisionId": r["decisionId"],
                        "handId": r["handId"],
                        "trickNo": r["trickNo"],
                        "played": r["played"],
                        "suggested": r.get("suggested"),
                        "alternatives": r["alternatives"],
                    }
                    for r in rows
                ],
            }
            for k, rows in sorted(rule_buckets.items(), key=lambda kv: -len(kv[1]))
        ],
        "casesOurBot": cases_us,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    print(f"packs with decisions: {len(packs)}, skipped: {len(skipped)}")
    for s in pack_stats:
        print(
            f"  {s['file']}: our better={s['ourBetter']} "
            f"(miss={s['ourMissed']} over={s['ourOverpay']}) "
            f"all={s['allBetter']}"
        )
    print(
        f"TOTAL our better={n_us} "
        f"(miss={kind_count['missed_take']} over={kind_count['overpay']}) "
        f"all={n_all}"
    )
    for k, rows in sorted(rule_buckets.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"  {len(rows):4}  {k}")


if __name__ == "__main__":
    main()
