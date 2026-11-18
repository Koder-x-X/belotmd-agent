"""Импорт frames*.jsonl в структурированный журнал решений.

Для старых записей явно указывает, что восстановлено, а что отсутствует.
Пассивная запись чужой ручной сессии через протокол не поддерживается
безопасно (вторая сессия может вытеснить игрока) — используйте импорт.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .features import FEATURE_VERSION
from .journal import JOURNAL_FORMAT, snapshot_state
from . import paths as _paths
from ..platform.sync import StateSynchronizer


def import_frames(frames_path, out_path=None, my_player_id=None) -> dict:
    _paths.ensure_dirs()
    frames_path = Path(frames_path)
    if out_path is None:
        out_path = _paths.JOURNALS_DIR / f"import-{frames_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out_path = Path(out_path)

    sync = StateSynchronizer()
    session_id = uuid.uuid4().hex[:12]
    n_frames = 0
    n_decisions = 0
    missing = {
        "send_attempts": "не восстанавливается из frames",
        "compute_ms": "не восстанавливается из frames",
        "intent_vs_confirm": "только наблюдаемое изменение состояния",
        "other_hands": "скрыты; только длина/belief",
    }
    recovered = [
        "phase", "hand", "trump", "trick", "graveyard",
        "legal_mask (по правилам state)", "our_action (по исчезновению карты / смене хода)",
    ]

    prev_sig = None
    prev_state_snap = None
    prev_seat = None
    prev_mask = None
    prev_scores = None
    pending = None

    with open(frames_path, encoding="utf-8") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        header = {
            "format": JOURNAL_FORMAT,
            "type": "import_meta",
            "source_frames": str(frames_path),
            "session_id": session_id,
            "t": time.time(),
            "recovered": recovered,
            "missing": missing,
            "feature_version": FEATURE_VERSION,
            "note": "Импорт frames: маска восстановлена по правилам, "
                    "не получена от сервера как отдельное поле.",
        }
        fout.write(json.dumps(header, ensure_ascii=False) + "\n")

        for line in fin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            raw = rec.get("state") or rec
            pid = my_player_id or rec.get("pid")
            sync.sync(raw, pid)
            n_frames += 1
            st = sync.state
            me = sync.my_pos
            if me is None:
                continue

            # Сигнатура хода
            trick = tuple((s, c) for s, c in (st.current_trick or []))
            sig = (st.phase, st.tricks_played, trick, st.current_player,
                   tuple(sorted(st.hands[me])) if me < len(st.hands) else ())

            # Если наш ход и маска непустая — потенциальное решение
            if st.current_player == me and st.phase in ("BIDDING", "PLAYING") and not st.done:
                mask = st.get_legal_actions()
                if mask.any():
                    pending = {
                        "seat": me,
                        "mask": [int(i) for i in mask.nonzero()[0]],
                        "snap": snapshot_state(st, me),
                        "scores": list(sync.match_scores or [0, 0]),
                        "hand_id": getattr(sync, "hand_id", None),
                        "t": rec.get("t", time.time()),
                        "phase": st.phase,
                    }

            # Обнаружить подтверждённое действие: карта исчезла из руки
            if prev_state_snap is not None and prev_seat == me and pending:
                prev_cards = set()
                for hv in prev_state_snap.get("hands") or []:
                    if hv.get("known"):
                        prev_cards = set(hv.get("cards") or [])
                        break
                now_cards = set(st.hands[me]) if me < len(st.hands) else set()
                played = prev_cards - now_cards
                action = None
                if len(played) == 1:
                    action = int(next(iter(played)))
                elif st.phase != pending.get("phase") and pending.get("phase") == "BIDDING":
                    # Торговля: точное действие из frames восстановить сложно
                    action = None
                    decision = {
                        "format": JOURNAL_FORMAT,
                        "feature_version": FEATURE_VERSION,
                        "decision_id": f"{session_id}-imp-{n_decisions}",
                        "session_id": session_id,
                        "hand_id": pending.get("hand_id"),
                        "t": pending.get("t"),
                        "seat": me,
                        "source": "import_frames",
                        "phase": pending.get("phase"),
                        "state": pending["snap"],
                        "match_scores_before": pending["scores"],
                        "legal_actions": pending["mask"],
                        "mask_origin": "reconstructed_rules",
                        "chosen_action": None,
                        "confirmed": False,
                        "incomplete": True,
                        "import_note": "действие торговли не восстановлено однозначно",
                    }
                    fout.write(json.dumps(decision, ensure_ascii=False) + "\n")
                    n_decisions += 1
                    pending = None
                if action is not None:
                    decision = {
                        "format": JOURNAL_FORMAT,
                        "feature_version": FEATURE_VERSION,
                        "decision_id": f"{session_id}-imp-{n_decisions}",
                        "session_id": session_id,
                        "hand_id": pending.get("hand_id"),
                        "t": pending.get("t"),
                        "seat": me,
                        "num_players": int(getattr(st, "num_players", 4) or 4),
                        "source": "import_frames",
                        "phase": pending.get("phase"),
                        "state": pending["snap"],
                        "match_scores_before": pending["scores"],
                        "legal_actions": pending["mask"],
                        "mask_origin": "reconstructed_rules",
                        "chosen_action": action,
                        "confirmed_action": action,
                        "confirmed": True,
                        "send_attempts": None,
                        "compute_ms": None,
                        "incomplete": False,
                        "import_note": "восстановлено из исчезновения карты в руке",
                    }
                    fout.write(json.dumps(decision, ensure_ascii=False) + "\n")
                    n_decisions += 1
                    pending = None

            prev_sig = sig
            prev_state_snap = snapshot_state(st, me)
            prev_seat = me
            prev_scores = list(sync.match_scores or [0, 0])

    report = {
        "ok": True,
        "frames": n_frames,
        "decisions_written": n_decisions,
        "out": str(out_path),
        "recovered": recovered,
        "missing": missing,
    }
    return report


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="belot-import-frames")
    p.add_argument("frames")
    p.add_argument("-o", "--out", default=None)
    args = p.parse_args(argv)
    r = import_frames(args.frames, args.out)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
