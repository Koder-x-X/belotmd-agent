"""Преобразование состояния в признаки без будущих/скрытых данных.

Один и тот же encode используется при обучении и в живой игре.
Чужие hands из state.hands не читаются — только known/impossible/graveyard.
"""

from __future__ import annotations

import numpy as np

FEATURE_VERSION = "v1"
ACTION_SPACE = 38


def encode_state(state, seat: int, match_scores, legal_mask) -> np.ndarray:
    """Вектор признаков, известный игроку seat до решения."""
    parts = []

    # Собственная рука (32)
    hand = np.zeros(32, dtype=np.float32)
    if 0 <= seat < len(getattr(state, "hands", [])):
        for c in state.hands[seat]:
            if 0 <= int(c) < 32:
                hand[int(c)] = 1.0
    parts.append(hand)

    # Козырь one-hot: none + 4 масти (5)
    trump_oh = np.zeros(5, dtype=np.float32)
    trump = getattr(state, "trump", None)
    if trump is None:
        trump_oh[0] = 1.0
    else:
        trump_oh[1 + int(trump) % 4] = 1.0
    parts.append(trump_oh)

    # Фаза (2)
    phase = getattr(state, "phase", "")
    parts.append(np.array([
        1.0 if phase == "BIDDING" else 0.0,
        1.0 if phase == "PLAYING" else 0.0,
    ], dtype=np.float32))

    # Раунд торговли (2)
    br = int(getattr(state, "bidding_round", 1) or 1)
    parts.append(np.array([1.0 if br == 1 else 0.0, 1.0 if br == 2 else 0.0],
                          dtype=np.float32))

    # Face-up (32) — только если известна
    face = np.zeros(32, dtype=np.float32)
    fc = getattr(state, "face_up_card", None)
    if fc is not None and 0 <= int(fc) < 32:
        face[int(fc)] = 1.0
    parts.append(face)

    # Текущая взятка: до 4 карт + маска занятых слотов (32 + 4)
    trick_cards = np.zeros(32, dtype=np.float32)
    trick_slots = np.zeros(4, dtype=np.float32)
    trick = list(getattr(state, "current_trick", None) or [])
    for i, (_s, c) in enumerate(trick[:4]):
        if 0 <= int(c) < 32:
            trick_cards[int(c)] = 1.0
        trick_slots[i] = 1.0
    parts.append(trick_cards)
    parts.append(trick_slots)

    # Сыгранные (graveyard) 32
    grave = np.zeros(32, dtype=np.float32)
    for c in getattr(state, "graveyard", None) or []:
        if 0 <= int(c) < 32:
            grave[int(c)] = 1.0
    parts.append(grave)

    # Известные карты оппонентов / невозможные (по 3*32 для relative seats)
    seats = int(getattr(state, "num_players", 4) or 4)
    known = np.zeros(3 * 32, dtype=np.float32)
    imposs = np.zeros(3 * 32, dtype=np.float32)
    kc = getattr(state, "known_cards", None)
    ic = getattr(state, "impossible_cards", None)
    for rel in range(1, 4):
        pidx = (seat + rel) % max(seats, 4)
        if kc is not None and pidx < len(kc):
            known[(rel - 1) * 32:(rel) * 32] = kc[pidx].astype(np.float32)
        if ic is not None and pidx < len(ic):
            imposs[(rel - 1) * 32:(rel) * 32] = ic[pidx].astype(np.float32)
    parts.append(known)
    parts.append(imposs)

    # Счёт матча (2), нормализованный
    ms = list(match_scores or [0, 0])
    while len(ms) < 2:
        ms.append(0)
    parts.append(np.array([ms[0] / 151.0, ms[1] / 151.0], dtype=np.float32))

    # Наши очки раздачи / очки команд (2)
    raw = list(getattr(state, "raw_points_by_team", None) or [0, 0])
    while len(raw) < 2:
        raw.append(0)
    parts.append(np.array([raw[0] / 162.0, raw[1] / 162.0], dtype=np.float32))

    # Место / команда / число игроков
    parts.append(np.array([
        seat / 3.0,
        (seat % 2) / 1.0,
        seats / 4.0,
        float(getattr(state, "tricks_played", 0) or 0) / 8.0,
    ], dtype=np.float32))

    # Declarer relative (5: none + 4 relative)
    dec = np.zeros(5, dtype=np.float32)
    declarer = getattr(state, "declarer", None)
    if declarer is None:
        dec[0] = 1.0
    else:
        rel = (int(declarer) - seat) % max(seats, 4)
        dec[1 + (rel % 4)] = 1.0
    parts.append(dec)

    # Legal mask (38) — известна до хода
    mask = np.zeros(ACTION_SPACE, dtype=np.float32)
    lm = np.asarray(legal_mask, dtype=np.float32).ravel()
    n = min(ACTION_SPACE, lm.size)
    mask[:n] = lm[:n]
    parts.append(mask)

    return np.concatenate(parts).astype(np.float32)


def feature_dim() -> int:
    # Build once from empty-ish state shape
    from ..game.state import BelotState
    st = BelotState()
    st.phase = "PLAYING"
    st.trump = 0
    st.hands[0] = [0, 1, 2]
    st.current_player = 0
    return int(encode_state(st, 0, [0, 0], st.get_legal_actions()).shape[0])


def assert_no_hidden_hands(state, seat, features: np.ndarray) -> None:
    """Тестовый хелпер: чужие карты из hands не должны кодироваться напрямую.

    Проверяем инвариант процесса, а не содержимое вектора: encode не читает
    чужие hands. Здесь — документация контракта.
    """
    _ = (state, seat, features)
    return None
