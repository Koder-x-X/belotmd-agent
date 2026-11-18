# Fallen3 — mid-trick EXP + deeper expected points

## Цель

Один боевой агент сильнее `fallen2`: учесть уроки из pack/history
(«не взяли / переплата») и чаще доверять EV-поиску.

## Что взято из данных

- `docs/fallen-missed-takes-lessons.md` / pack решений: много пропущенных
  взятий **не только последним** ходом → baseline EXP как у `fallen21`
  (EXP3-Take / EXP3-Cheap на любом ответе, если партнёр не выигрывает).
- EV-поиск Fallen2 v3 (`fallen2_points.evaluate_legal_moves`) с более
  глубокими дефолтами.

## Алгоритм хода (PLAY)

1. Baseline = Fallen + mid-trick EXP3.
2. Сэмпл миров + playout Strong → expected points.
3. Override baseline, если `EV_best - EV_baseline ≥ FALLEN3_MARGIN_PTS`.
4. При сбое поиска — только baseline.

Торговля — как Fallen/Strong.

## Дефолты (env)

| Переменная | По умолчанию | Смысл |
|------------|--------------|--------|
| `FALLEN3_MARGIN_PTS` | 1.0 | порог смены (ниже, чем у fallen2=2) |
| `FALLEN3_WORLDS` | 16 | миры (+8 при ≤3 картах) |
| `FALLEN3_SEARCH_MS` | 520 | бюджет поиска |
| `time_budget_ms` | 1000 | общий бюджет |

## Версия

`3.1.2` · desk tip: `desk/LATEST_AGENT` → `fallen3`

### 3.1.2 — EV на 3p

Belief/determinization раньше всегда считал 4 места → на 3-местных
столах `IndexError` → `EV-Fallback (no_determinization)` почти на каждом
ходе. Исправлено: `constraints` / `UniformSampler` уважают `num_players`.
Если сэмпл всё же невозможен — heuristic eval вместо пустого fallback.
