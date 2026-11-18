"""
bot.py — the live belot.md player.

Owns everything that is a *rule of the platform*: when it is our turn, when to
send READY, when the seven-swap window is open, which declarations to announce,
whether the server actually accepted the card we sent, and whether belot.md has
quietly handed our seat to its own bot after a timeout.

It owns no policy. Which card to play and what to bid come from an `Agent`
(see `belotmd.agents.base`), which this module knows only through
`reset()` / `snapshot()` / `restore()` / `act()`. Nothing here imports torch or
knows what an observation vector looks like.
"""

import asyncio
import json
import os
import signal
import time
from pathlib import Path

import numpy as np

from .agents import get_agent
from .audit import Auditor, FrameRecorder
from .config import Config
from .game import actions, combinations as combo
from .game.actions import (ACTION_ACCEPT, ACTION_PASS, CARD_ACTIONS,
                           SUIT_ACTIONS, suit_of)
from .platform.client import BelotClient
from .platform.protocol import (ASCII_TO_ID, BID_PHASES, CHAR_TO_CARD,
                                DEAL_PHASES, END_PHASES, GAME_ENDED,
                                GAME_TYPE_TOURNAMENT, ID_TO_ASCII,
                                NOT_STARTED, PLAY, SWAP_SEVEN, DEAL_CARDS_2,
                                between_matches)
from .platform.sync import StateSynchronizer, table_seats


class LiveBelotBot:
    """Plays one belot.md session with the supplied agent."""

    # Shortest gap between two READY sends while the server has not yet
    # acknowledged the first.
    READY_RESEND_S = 1.0

    # Seat rotation, when playing as a pair. A rotation drops the other three
    # players (close code 4005) and they rejoin, so it needs a gap; and since
    # three other seats cycle, two sends are enough to reach any arrangement.
    # The cap exists because the payload is unverified: if it does something
    # other than what we think, this stops after six tries instead of
    # shuffling the table forever.
    ROTATE_RETRY_S = 3.0
    # Two rotations place any partner opposite (the three non-sender seats form
    # a 3-cycle), so a third means the rule we measured did not hold and
    # something about the table is not what we think. Stop and say so rather
    # than shuffling a table full of people indefinitely.
    MAX_ROTATIONS = 3
    # A probe rotation drops the other three players, who then rejoin, so the
    # experiment needs a wider gap than the ordinary fix-the-seats path.
    ROTATE_PROBE_GAP_S = 8.0

    # How long a FULL table gets to start a match before we give up on it and
    # make another. Armed the first time the table fills and never re-armed:
    # if it were restarted whenever somebody left and was replaced, a table
    # that churns one seat forever would never time out at all.
    # 7s was too short — humans sit down and get kicked before they ready,
    # so the table looks "spoiled" the moment it fills.
    TABLE_START_GRACE_S = 60.0
    # Every seat taken and none of them our partner's. A moment's patience
    # first: our partner may be a second behind the strangers, and deleting
    # ejects three people who just sat down.
    FULL_WITHOUT_PARTNER_GRACE_S = 5.0
    # Our partner never turned up at all and the table never filled either --
    # a quiet lobby, or a guest that cannot see us. Matches the guest's own
    # window (20 looks x 2s), so both sides give up at about the same moment.
    NO_PARTNER_GIVE_UP_S = 40.0
    # Deleting a table ejects three people. A persistent problem must not turn
    # into an endless create-and-delete loop.
    MAX_RECREATES = 5
    # A REMOVE_PLAYER the server has not reflected yet. Retrying every frame
    # would flood the room; a few seconds is enough to see the seat empty.
    KICK_RETRY_S = 3.0

    # Class-level defaults, so a bot built any other way than through
    # __init__ -- the tests construct one with __new__ -- is simply a bot
    # playing alone, rather than an AttributeError on the first frame.
    partner = None
    is_host = False
    rotation_probe = 0
    leave_probe = 0
    # Counted across tables. Class-level so that a bot built any other way than
    # through __init__ still has them; all are immutable, and __init__ gives
    # every real instance its own.
    _recreates = 0
    _recreate_capped = False
    _leave_probes_done = 0
    # Set by whoever is stopping this bot (a supervisor, a deadline). While it
    # is set, the bot never announces READY for another match, so the table
    # cannot start one it would then have to walk out of.
    stop_requested = False

    def __init__(self, config: Config = None, agent=None, agent_kwargs=None):
        self.config = config or Config.from_env()
        self.client = BelotClient(
            cookies=self.config.require_cookies(),
            reconnect=self.config.reconnect,
            retry_delay_s=self.config.retry_delay_s,
            rejoin_delay_s=self.config.rejoin_delay_s,
            table_mode=self.config.table_mode,
            table_id=self.config.table_id or None,
            table_creator=self.config.table_creator or None,
            table_password=self.config.table_password or None,
            table_points=self.config.table_points or None,
            join_poll_s=self.config.join_poll_s,
            join_max_polls=self.config.join_max_polls,
            max_create_failures=self.config.max_create_failures,
            pair_restart_pause_s=self.config.pair_restart_pause_s,
            tournament_min_status=self.config.tournament_min_status,
            tournament_enroll=self.config.tournament_enroll,
        )
        # VDS/zombie rooms: gameplay_new still names a dead match; rejoining
        # shows "playing" in the desk while we never act. Prefer a fresh table.
        skip = (os.environ.get("BELOT_SKIP_ACTIVE") or "").strip().lower()
        skip_flag = Path(
            os.environ.get("BELOT_SKIP_ACTIVE_FILE")
            or (Path(os.environ.get("BELOT_STOP_FILE") or "x").parent
                / "skip_active.once")
        )
        if skip_flag.is_file():
            skip = "1"
            try:
                skip_flag.unlink()
            except OSError:
                pass
        if skip in ("1", "true", "yes", "on"):
            self.client._skip_active_once = True
            # Guests must keep joining the host — never flip to create.
            if (self.config.table_mode or "") != "join":
                self.client._create_next = True
            print("[Bot] Fresh table: skip rejoining any active room.")
        self.sync_engine = StateSynchronizer()

        # The decision-maker. Anything satisfying belotmd.agents.base.Agent
        # works; nothing below this line knows which one it got. Pass one in
        # directly, or name it in the config and let the registry build it.
        if agent is None:
            kwargs = dict(agent_kwargs or {})
            if self.config.checkpoint and "checkpoint" not in kwargs:
                kwargs["checkpoint"] = self.config.checkpoint
            agent = get_agent(self.config.agent, **kwargs)
        self.agent = agent
        print(f"[Bot] Agent: {getattr(self.agent, 'name', type(self.agent).__name__)}")
        print("[Bot] 3-player build: only tables of three.")
        if self.config.table_points in ("51", "101"):
            print(f"[Bot] 3-player tables to {self.config.table_points} only.")
        else:
            print("[Bot] 3-player tables to 51 or 101.")

        # Playing as a pair, and whether we are the one who made the table.
        # Only the creator can move players between seats.
        self.partner = (self.config.partner or "").strip().casefold() or None
        self.is_host = self.config.table_mode == "create"
        self.rotation_probe = int(getattr(self.config, "rotation_probe", 0) or 0)
        self.leave_probe = int(getattr(self.config, "leave_probe", 0) or 0)
        if self.leave_probe and self.is_host:
            print(f"[Bot] LEAVE PROBE: will abandon the first "
                  f"{self.leave_probe} table(s) on purpose once our partner "
                  f"sits down, to prove the leave-and-recreate path.")
        if self.rotation_probe and self.is_host:
            print(f"[Bot] ROTATION PROBE: will rotate the seats "
                  f"{self.rotation_probe}x before playing, whatever the "
                  f"layout, and log what moves.")
        elif self.rotation_probe:
            print("[Bot] Rotation probe ignored: only the table's creator can "
                  "move players between seats.")
        if self.partner:
            print(f"[Bot] Paired run: holding READY until our partner sits "
                  f"opposite ({'host' if self.is_host else 'guest'}).")

        # Counted across tables, not per table: five bad tables in a row is a
        # problem with us, not with the tables. Reset as soon as a hand is
        # actually dealt somewhere.
        self._recreates = 0
        self._recreate_capped = False
        self._leave_probes_done = 0
        # Set when we walk out of someone else's table in order to make our
        # own. Applied on the NEXT table, not this one: flipping is_host while
        # we are still seated at theirs would make us send REMOVE_PLAYER in a
        # room where we are not the creator.
        self._host_next_table = False

        # After-match profile scrape disabled by default (belot.md IP bans).
        flag = (os.environ.get("BELOT_PROFILE_AFTER_MATCH") or "").strip().lower()
        self.profile_after_match = flag in ("1", "true", "yes", "on")
        self._profile_leave_key = None
        self._history_login = (os.environ.get("BELOT_HISTORY_LOGIN") or "").strip()
        if self.profile_after_match:
            print("[Bot] После каждой партии: выход → снимок рейтинга → новый стол.")

        self._room_seq = 0
        self._reset_room_state()

        self.audit = self.config.audit
        if self.audit:
            self.recorder = FrameRecorder(self.config.frames_path)
            self.auditor = Auditor(verbose=True)
            print(f"[Bot] AUDIT mode on: recording frames to "
                  f"{self.config.frames_path}")

        self.journal = None
        self._journal_decision_id = None
        self._history_pending = None
        if getattr(self.config, "decision_journal", True):
            from .learning.journal import DecisionJournal
            jpath = getattr(self.config, "decisions_path", "") or None
            self.journal = DecisionJournal(path=jpath)
            agent_desc = getattr(self.agent, "describe", None)
            strat = agent_desc() if callable(agent_desc) else getattr(self.agent, "name", "?")
            print(f"[Bot] Decision journal: {self.journal.path} "
                  f"(strategy={strat})")

        self.history = None
        hist_root = os.environ.get("BELOT_HISTORY_DIR") or ""
        if hist_root:
            try:
                from .history import GameHistoryRecorder, stable_bot_id
                login = (
                    os.environ.get("BELOT_HISTORY_LOGIN")
                    or getattr(self.config, "username", "")
                    or ""
                )
                bot_id = os.environ.get("BELOT_HISTORY_BOT_ID") or stable_bot_id(login)
                self.history = GameHistoryRecorder(
                    root=hist_root,
                    bot_id=bot_id,
                    login=login,
                    agent=getattr(self.agent, "name", type(self.agent).__name__),
                    agent_version=str(
                        getattr(self.agent, "agent_version", None)
                        or getattr(self.agent, "version", "")
                        or ""
                    ),
                    model_version=str(getattr(self.agent, "model_version", "") or ""),
                    variant=os.environ.get("BELOT_HISTORY_VARIANT") or "3",
                    points=os.environ.get("BELOT_TABLE_POINTS") or "",
                )
                print(f"[Bot] Game history: {self.history.bot_dir}")
            except Exception as exc:
                print(f"[HISTORY][ERROR] init failed: {exc}")
                self.history = None

    # `act` is the only method an agent must supply. reset/snapshot/restore
    # exist for agents that carry state across turns, and the docs promise they
    # are no-ops otherwise -- so a stateless agent should not have to write
    # three empty methods just to avoid an AttributeError on the first hand
    # boundary. Looked up per call rather than bound at construction, so that
    # no construction path can forget to wire them.
    def _agent_reset(self):
        fn = getattr(self.agent, "reset", None)
        if fn is not None:
            fn()

    def _agent_snapshot(self):
        fn = getattr(self.agent, "snapshot", None)
        return fn() if fn is not None else None

    def _agent_restore(self, snapshot):
        fn = getattr(self.agent, "restore", None)
        if fn is not None:
            fn(snapshot)

    def _announce_table(self, raw_state, my_pos):
        """One line per scored hand, and a win or loss when the match ends.

        The panel reads this line. Names stay here because the desk is local;
        the auditor's seat map still uses letters.
        """
        phase = raw_state.get("currentPhase", 0)
        if phase not in END_PHASES:
            return
        hand = getattr(self.sync_engine, "hand_id", 0)
        key = (hand, phase)
        if getattr(self, "_announced_key", None) == key:
            return
        self._announced_key = key
        seats = int(getattr(self.sync_engine, "seats", 4) or 4)
        columns = 3 if seats == 3 else 2
        scores = self._match_scores(raw_state, columns)
        points, combos = self._round_parts(raw_state, columns)
        capot = "да" if points and min(points) == 0 and max(points) > 0 else "нет"
        score_text = ":".join(str(n) for n in scores) if scores else "—"
        points_text = ":".join(str(n) for n in points) if points else "—"
        combos_text = ":".join(str(n) for n in combos) if combos else "—"
        places = self._place_names(raw_state, my_pos, seats)
        result = "игра"
        if phase == GAME_ENDED and scores and 0 <= (my_pos if seats == 3 else my_pos % 2) < len(scores):
            ours = scores[my_pos if seats == 3 else my_pos % 2]
            best = max(scores)
            try:
                target = int(raw_state.get("targetScore") or 0)
            except (TypeError, ValueError):
                target = 0
            if ours == best and scores.count(best) == 1 and (not target or best >= target):
                result = "победа"
            elif ours < best:
                result = "поражение"
        rnd = raw_state.get("round") or hand
        print(
            f"[стол] раздача {rnd} | партия {score_text} | очки {points_text} | "
            f"комбинации {combos_text} | капот {capot} | места {places} | итог {result}"
        )
        board = self._score_board(raw_state, my_pos, seats)
        if board:
            print(f"[стол] табло {json.dumps(board, ensure_ascii=False)}")
        hist = getattr(self, "history", None)
        if hist is not None:
            try:
                hist.note_hand_end(
                    hand_id=hand,
                    scores=scores,
                    points=points,
                    result=result,
                )
                try:
                    from .history.reconstruct import reconstruct_hand
                    from .history.recorder import read_jsonl_tolerant
                    rows = read_jsonl_tolerant(hist.path) if hist.path else []
                    seats_n = int(getattr(self.sync_engine, "seats", 4) or 4)
                    recon = reconstruct_hand(
                        rows, hand_id=hand, seats=seats_n, our_seat=my_pos,
                    )
                    hist.note_analysis({
                        "handId": hand,
                        "reconstructionStatus": recon.get("status"),
                        "reconstruction": recon,
                        "analysisStatus": "pending_pack",
                    })
                except Exception as exc2:
                    print(f"[HISTORY][ERROR] reconstruct: {exc2}")
                if phase == GAME_ENDED and result in ("победа", "поражение"):
                    hist.finalize("completed", result=result, scores=scores, reason="match_end")
            except Exception as exc:
                print(f"[HISTORY][ERROR] announce: {exc}")

    def _score_board(self, raw_state, my_pos, seats):
        """Табло как на belot.md: имена колонок + сырые строки scoreTable."""
        from .platform.sync import _bolt_marker, _to_int

        players = raw_state.get("players") or []
        names = []
        for seat in range(seats):
            player = players[seat] if seat < len(players) else {}
            if seat == my_pos:
                names.append("мы")
            else:
                label = str(player.get("name") or "игрок").replace("|", " ").strip() or "игрок"
                if player.get("bot"):
                    label += " (бот)"
                names.append(label)
        raw = raw_state.get("scoreTable", "[]")
        try:
            table = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
        except Exception:
            return None
        if not isinstance(table, list) or not table:
            return None
        rows = []
        for row in table:
            if not isinstance(row, (list, tuple)):
                continue
            cells = []
            for cell in list(row)[:seats]:
                n = _bolt_marker(cell)
                if n is not None:
                    cells.append(f"БТ-{n}")
                else:
                    cells.append(str(_to_int(cell, 0)))
            if len(cells) == seats:
                rows.append(cells)
        if not rows:
            return None
        return {"names": names, "rows": rows}

    def _match_scores(self, raw_state, columns):
        raw = raw_state.get("scoreTable", "[]")
        try:
            table = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
            scores, _bolts = StateSynchronizer._decode_score_table(table, columns)
        except Exception:
            return []
        return scores if any(scores) or table else []

    def _round_parts(self, raw_state, columns):
        raw = raw_state.get("roundTotals", "[]")
        try:
            totals = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return [], []
        if not isinstance(totals, list) or len(totals) < columns:
            return [], []
        points, combos = [], []
        for cell in totals[:columns]:
            if not isinstance(cell, dict):
                return [], []
            try:
                points.append(int(cell.get("p") or 0))
                combos.append(int(cell.get("c") or 0))
            except (TypeError, ValueError):
                return [], []
        return points, combos

    def _place_names(self, raw_state, my_pos, seats):
        players = raw_state.get("players") or []
        names = []
        for seat in range(seats):
            player = players[seat] if seat < len(players) else {}
            if seat != my_pos and not player.get("id"):
                continue
            if seat == my_pos:
                label = "мы"
            else:
                label = str(player.get("name") or "игрок").replace("|", " ").strip() or "игрок"
            if player.get("bot") and seat != my_pos:
                label += " (бот сайта)"
            names.append(label)
        return ", ".join(names) if names else "—"

    def _seat_label(self, raw_state, seat, my_pos) -> str:
        """Имя за столом для журнала ходов: «мы» или логин соперника."""
        if seat == my_pos:
            return "мы"
        players = raw_state.get("players") or []
        player = players[seat] if isinstance(players, list) and seat < len(players) else {}
        if not isinstance(player, dict):
            player = {}
        label = str(player.get("name") or "игрок").replace("|", " ").strip() or "игрок"
        if player.get("bot"):
            label += " (бот)"
        return label

    def _live_seat_name(self, raw_state, seat, my_pos) -> str:
        """Для [LIVE]/ stdout без чужих логинов (роли / буквы)."""
        if seat == my_pos:
            return "мы"
        players = raw_state.get("players") or []
        player = players[seat] if isinstance(players, list) and seat < len(players) else {}
        if not isinstance(player, dict):
            player = {}
        name = str(player.get("name") or "").strip()
        partner = getattr(self, "partner", None)
        if partner and name.casefold() == partner:
            return "partner"
        # отдельный словарь — не трогаем _seat_labels пробы
        labels = getattr(self, "_live_labels", None)
        if not isinstance(labels, dict):
            labels = {}
            self._live_labels = labels
        key = name.casefold() or f"seat{seat}"
        if key not in labels:
            used = set(labels.values())
            for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if ch not in used:
                    labels[key] = ch
                    break
            else:
                labels[key] = f"P{seat}"
        return labels[key]

    def _log_trick_card(self, raw_state, my_pos, seat, card) -> None:
        ch = ID_TO_ASCII.get(card, "?")
        eng = CHAR_TO_CARD.get(ch, ch)
        who = self._seat_label(raw_state, seat, my_pos)
        print(f"[Trick] {who} plays {eng}")

    def _history_play(self, raw_state, my_pos, seat, card, hand, trick_no) -> None:
        hist = getattr(self, "history", None)
        if hist is None:
            return
        try:
            hist.note_play(
                seat=int(seat),
                card=int(card),
                card_token=self._card_token(card),
                us=int(seat) == int(my_pos),
                hand_id=hand,
                trick_no=trick_no,
            )
        except Exception as exc:
            print(f"[HISTORY][ERROR] play: {exc}")

    def _history_touch_match(self, raw_state, my_pos) -> None:
        hist = getattr(self, "history", None)
        if hist is None:
            return
        phase = raw_state.get("currentPhase", 0)
        if phase in (NOT_STARTED,) or between_matches(phase):
            return
        try:
            seats = int(getattr(self.sync_engine, "seats", 4) or 4)
            room = getattr(self.client, "room_id", None)
            table = getattr(self.client, "table_id", None) or getattr(self.client, "_last_table_id", None)
            match_key = f"{table or 't'}:{room or self._room_seq}"
            hist.ensure_match(
                match_key=match_key,
                seat=int(my_pos),
                seats=seats,
                room_id=room,
                table_id=table,
            )
            state = self.sync_engine.state
            trump = getattr(state, "trump", None)
            declarer = getattr(state, "declarer", None)
            if trump is not None:
                hist.note_trump(
                    trump=trump,
                    declarer=declarer,
                    open_flag=bool(getattr(state, "declarer_has_played_trump", False)),
                    hand_id=getattr(self.sync_engine, "hand_id", None),
                )
        except Exception as exc:
            print(f"[HISTORY][ERROR] match: {exc}")

    def _capture_recent_tricks(self, raw_state, my_pos) -> list:
        """Последние 3 закрытые взятки — для панели «что тупил бот»."""
        state = self.sync_engine.state
        hand = int(getattr(self.sync_engine, "hand_id", 0) or 0)
        tricks_done = int(getattr(state, "tricks_played", 0) or 0)
        seats = int(getattr(state, "num_players", 4) or 4)
        store = getattr(self, "_recent_tricks", None)
        if not isinstance(store, dict) or store.get("hand") != hand:
            store = {"hand": hand, "seen": 0, "items": []}
            self._recent_tricks = store
        last = [(int(s), int(c)) for s, c in (getattr(state, "last_trick", None) or [])]
        if tricks_done < int(store.get("seen") or 0):
            store["seen"] = tricks_done
            store["items"] = []
        if tricks_done > int(store.get("seen") or 0) and len(last) == seats:
            winner = None
            try:
                winner = self.sync_engine._trick_winner(last)
            except Exception:
                winner = None
            rows = [
                {
                    "seat": seat,
                    "name": self._seat_label(raw_state, seat, my_pos),
                    "us": seat == my_pos,
                    "id": int(card),
                    "card": self._card_token(card),
                }
                for seat, card in last
            ]
            store["items"].append({
                "n": tricks_done,
                "winner": winner,
                "winnerName": (
                    self._seat_label(raw_state, winner, my_pos)
                    if isinstance(winner, int) else None
                ),
                "usWon": isinstance(winner, int) and winner == my_pos,
                "cards": rows,
            })
            store["items"] = store["items"][-3:]
            store["seen"] = tricks_done
        return list(store.get("items") or [])

    def _card_token(self, card: int) -> str:
        ch = ID_TO_ASCII.get(int(card), "?")
        return CHAR_TO_CARD.get(ch, ch)

    def _combo_items_ru(self, value: str) -> list[dict]:
        """Токены объявлений → читаемые ярлыки для панели."""
        items = []
        for type_id, ch in combo.parse_field(value):
            label = None
            pts = 0
            if type_id == combo.TART:
                label, pts = "терц", 20
            elif type_id == combo.JUMATE_DE_SUTA:
                label, pts = "полсотни", 50
            elif type_id == combo.O_SUTA:
                label, pts = "сто", 100
            elif type_id == combo.PATRU_CARTI:
                rank = combo.four_rank(type_id, ch, ASCII_TO_ID)
                pts = combo.FOUR_POINTS.get(rank, 100) if rank is not None else 100
                if rank == 2:
                    label = "4 девятки"
                elif rank == 4:
                    label = "4 валета"
                elif rank == 0:
                    label = "4 семёрки"
                elif rank == 1:
                    label = "4 восьмёрки"
                else:
                    rank_names = {3: "десятки", 5: "дамы", 6: "короли", 7: "тузы"}
                    label = f"4 {rank_names.get(rank, 'карты')}"
            elif type_id == combo.BELA:
                label, pts = "белла", 20
            elif type_id == combo.LESS_THAN_14:
                label, pts = "меньше 14", 0
            elif type_id == combo.BELOT_COMBO:
                label, pts = "белот", 1010
            elif type_id == combo.WIN_ALL_HANDS:
                label, pts = "все взятки", 0
            elif type_id == combo.SURRENDER_BT:
                label, pts = "сдача", 0
            else:
                label = f"тип {type_id}"
            items.append({
                "token": f"{type_id}{ch}",
                "label": label,
                "points": pts,
                "text": f"{label} ({pts})" if pts else label,
            })
        return items

    def _emit_live_table(self, raw_state, my_pos) -> None:
        """Снимок стола для панели: рука, взятка, козырь, объявления, счёт."""
        state = self.sync_engine.state
        seats = int(getattr(state, "num_players", 4) or 4)
        hand = list(state.hands[my_pos]) if 0 <= my_pos < len(state.hands) else []
        trick = [(int(s), int(c)) for s, c in (state.current_trick or [])]
        recent = self._capture_recent_tricks(raw_state, my_pos)
        declared = list(getattr(state, "combinations", ["", "", "", ""]) or [])
        while len(declared) < seats:
            declared.append("")
        combo_fp = tuple(declared[:seats])
        recent_fp = tuple(
            (int(item.get("n") or 0), tuple((r["seat"], r["id"]) for r in item.get("cards") or []))
            for item in recent
        )
        fp = (
            getattr(state, "phase", None),
            getattr(state, "trump", None),
            int(getattr(state, "tricks_played", 0) or 0),
            tuple(trick),
            tuple(sorted(hand)),
            raw_state.get("activePlayer"),
            raw_state.get("currentPhase"),
            combo_fp,
            getattr(state, "declarer", None),
            bool(getattr(state, "declarer_has_played_trump", False)),
            recent_fp,
        )
        if getattr(self, "_live_fp", None) == fp:
            return
        self._live_fp = fp

        columns = 3 if seats == 3 else 2
        scores = self._match_scores(raw_state, columns)
        raw_players = raw_state.get("players") or []
        declarer = getattr(state, "declarer", None)
        players = []
        declarations = []
        for seat in range(seats):
            player = (
                raw_players[seat]
                if isinstance(raw_players, list) and seat < len(raw_players)
                else {}
            )
            if not isinstance(player, dict):
                player = {}
            cards = list(state.hands[seat]) if seat < len(state.hands) else []
            field = declared[seat] if seat < len(declared) else (player.get("combinations") or "")
            items = self._combo_items_ru(field)
            combo_pts, bella_pts = combo.points(field, ASCII_TO_ID)
            name = self._live_seat_name(raw_state, seat, my_pos)
            entry = {
                "seat": seat,
                "name": name,
                "us": seat == my_pos,
                "bot": bool(player.get("bot")),
                "count": len(cards),
                "declarer": declarer is not None and seat == declarer,
                "combos": items,
                "comboPoints": int(combo_pts + bella_pts),
                "comboRaw": field or "",
            }
            if seat == my_pos:
                entry["cards"] = [
                    {"id": int(c), "card": self._card_token(c)} for c in sorted(cards)
                ]
            players.append(entry)
            for item in items:
                declarations.append({
                    "seat": seat,
                    "name": name,
                    "us": seat == my_pos,
                    **item,
                })

        trick_rows = [
            {
                "seat": seat,
                "name": self._live_seat_name(raw_state, seat, my_pos),
                "us": seat == my_pos,
                "id": int(card),
                "card": self._card_token(card),
            }
            for seat, card in trick
        ]

        decision = getattr(self.agent, "last_decision", None)
        if decision is not None and not isinstance(decision, dict):
            decision = None

        trump = getattr(state, "trump", None)
        suit_names = ["бубны", "черви", "трефы", "пики"]
        suit_glyphs = ["♦", "♥", "♣", "♠"]
        payload = {
            "t": time.time(),
            "phase": getattr(state, "phase", None),
            "wirePhase": raw_state.get("currentPhase"),
            "trump": trump,
            "trumpName": (
                f"{suit_glyphs[trump]} {suit_names[trump]}"
                if isinstance(trump, int) and 0 <= trump < 4 else "—"
            ),
            "trumpOpen": bool(getattr(state, "declarer_has_played_trump", False)),
            "declarer": declarer,
            "declarerName": (
                self._live_seat_name(raw_state, declarer, my_pos)
                if isinstance(declarer, int) else None
            ),
            "tricks": int(getattr(state, "tricks_played", 0) or 0),
            "handId": int(getattr(self.sync_engine, "hand_id", 0) or 0),
            "active": raw_state.get("activePlayer"),
            "me": int(my_pos),
            "seats": seats,
            "scores": list(scores or []),
            "players": players,
            "declarations": declarations,
            "trick": trick_rows,
            "recentTricks": recent,
            "hand": [
                {"id": int(c), "card": self._card_token(c)} for c in sorted(hand)
            ],
            "agent": getattr(self.agent, "name", ""),
            "decision": decision,
        }
        print(f"[LIVE]{json.dumps(payload, ensure_ascii=False)}", flush=True)

    def _announce_plays(self, raw_state, my_pos):
        """В журнал: кто какую карту положил на взятку (мы и соперники)."""
        state = self.sync_engine.state
        hand = int(getattr(self.sync_engine, "hand_id", 0) or 0)
        tricks_done = int(getattr(state, "tricks_played", 0) or 0)
        live = [(int(s), int(c)) for s, c in (state.current_trick or [])]
        prev = getattr(self, "_announced_trick", None)
        seen = set(prev[2]) if prev and prev[0] == hand else set()

        # Взятка закрылась: карты, которые не успели залогировать mid-trick.
        # trickNo должен совпадать с mid-trick play (prev[1]), а не с
        # уже увеличенным tricks_played — иначе play=0 / trick_end=1.
        if prev is not None and prev[0] == hand and tricks_done > prev[1]:
            closed_trick_no = int(prev[1])
            for seat, card in (getattr(state, "last_trick", None) or []):
                pair = (int(seat), int(card))
                if pair in seen:
                    continue
                self._log_trick_card(raw_state, my_pos, pair[0], pair[1])
                seen.add(pair)
                self._history_play(
                    raw_state, my_pos, pair[0], pair[1], hand, closed_trick_no,
                )
            # закрытая взятка
            last = [(int(s), int(c)) for s, c in (getattr(state, "last_trick", None) or [])]
            hist = getattr(self, "history", None)
            if hist is not None:
                try:
                    winner = None
                    try:
                        winner = self.sync_engine._trick_winner(last)
                    except Exception:
                        winner = None
                    hist.note_trick_end(
                        hand_id=hand,
                        trick_no=closed_trick_no,
                        cards=[
                            {
                                "seat": s,
                                "card": c,
                                "cardToken": self._card_token(c),
                                "us": s == my_pos,
                                "name": self._seat_label(raw_state, s, my_pos),
                            }
                            for s, c in last
                        ],
                        winner=winner,
                        winner_name=(
                            self._seat_label(raw_state, winner, my_pos)
                            if isinstance(winner, int) else None
                        ),
                    )
                except Exception as exc:
                    print(f"[HISTORY][ERROR] trick_end: {exc}")
                # серверное подтверждение нашего хода в этой взятке
                pending = getattr(self, "_history_pending", None)
                if pending is not None:
                    our_card = next((c for s, c in last if s == my_pos), None)
                    intent = pending.get("intent_action")
                    if intent is None:
                        intent = pending.get("chosen_action")
                    if our_card is not None and intent is not None and int(intent) == int(our_card):
                        try:
                            pending["confirmed_action"] = int(our_card)
                            pending["confirmed"] = True
                            pending["server_confirmed"] = True
                            pending["confirm_status"] = "server_confirmed"
                            pending["count_decision"] = True
                            hist.note_decision(pending)
                        except Exception as exc:
                            print(f"[HISTORY][ERROR] confirm: {exc}")
                        self._history_pending = None
            seen = set()

        if prev is None or prev[0] != hand or prev[1] != tricks_done:
            already = []
        else:
            already = list(prev[2])
        for i, (seat, card) in enumerate(live):
            if i < len(already):
                continue
            self._log_trick_card(raw_state, my_pos, seat, card)
            self._history_play(raw_state, my_pos, seat, card, hand, tricks_done)
        self._announced_trick = (hand, tricks_done, tuple(live))

    def _reset_room_state(self):
        """Everything that describes THE TABLE WE ARE AT, in one place.

        A long run moves between tables, and every one of these is meaningless
        at the next: a play in flight, a declaration already announced, a
        seven-swap window, whose turn we thought it was. Carrying any of them
        across a rejoin is the bug class that made the bot sit silently in a
        lobby -- so they are reset together, from a single place, rather than
        each being someone's job to remember.
        """
        # Turn debounce (replaces an older hard dedup, which deadlocked the
        # bot forever if the server ever rejected an action).
        self.last_action_turn_id = None
        self.last_dispatch_ts = 0.0
        # (turn_id, agent_snapshot_before_this_turn, {actions already refused})
        # Keeps agent state advancing once per DECISION rather than once per
        # DISPATCH, and blocks a refused action on re-dispatch.
        self._turn_decision = None
        self._ready_sent_ts = 0.0     # last READY, for the resend debounce
        self._declared = set()        # (hand_id, value) already announced
        self._pending_play = None     # (hand_id, trick_no, card) in flight
        self._pending_play_ts = 0.0   # monotonic when play was dispatched
        self._pending_bid = None      # (hand_id, action) — торговля ждёт сервер
        self._rejected_plays = 0
        self.seat_bot_controlled = False
        self._takeover_frame = None   # (round, phase) when the seat was lost
        self._swap_hand = None        # hand_id of the open phase-8 window
        self._swap_sent = False       # SWAP_SEVEN sent in that window
        self._swap_skip = False       # decided not to swap this window
        self._rotations = 0           # seat rotations sent at THIS table
        self._rotate_ts = 0.0         # last one, for the debounce
        self._seat_map = None         # last seat layout we reported
        self._probe_done = 0          # probe rotations sent at THIS table
        self._seat_labels = {}        # player id -> a letter, per table
        self._announced_key = None    # (hand, phase) already reported to the panel
        self._announced_trick = None  # (hand, tricks_done, cards) already logged
        self._recent_tricks = None    # last 3 closed tricks for teach panel
        self._live_fp = None          # fingerprint of last [LIVE] table emit
        self._full_since = None       # when this table FIRST filled up
        self._room_since = None       # when we arrived at this table
        self._leave_sent = False      # one LEAVE_TABLE per table, at most
        self._table_started = False   # has a hand been dealt here?
        self._kick_ts = {}            # seat -> last REMOVE_PLAYER, this table
        self._unready_since = {}      # player id -> when they sat down unready
        self._game_type_warned = False  # a non-tournament room, reported once
        # Keep _profile_leave_key across room reset so we don't double-leave
        # the same ended match while the leave is in flight.

    async def _refresh_belot_profile(self) -> None:
        """Parse place/★/status/coins/today after leaving the table; push to desk."""
        cookies = (os.environ.get("BELOT_COOKIES") or "").strip()
        login = self._history_login
        if not cookies:
            print("[профиль] нет BELOT_COOKIES — пропуск")
            return
        try:
            from .platform.account_profile import fetch_account_profile
        except Exception as exc:
            print(f"[профиль][ERROR] import: {exc}")
            return

        row = None
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(1.5)
            try:
                row = await asyncio.to_thread(
                    fetch_account_profile, cookies, login=login
                )
            except Exception as exc:
                print(f"[профиль][ERROR] fetch: {exc}")
                return
            if row.get("ok"):
                break
            if row.get("error") != "in_game":
                break
        if not row:
            return
        if row.get("ok"):
            print(
                f"[профиль] place={row.get('place')} stars={row.get('ratingStars')} "
                f"status={row.get('statusName')} coins={row.get('coins')} "
                f"today={row.get('todayWins')}/{row.get('todayGames')}"
            )
        else:
            print(f"[профиль] не снял: {row.get('error') or row.get('todayError') or '?'}")
        desk = (os.environ.get("BELOT_DESK_URL") or "http://127.0.0.1:8790").rstrip("/")
        payload = {
            "login": login or row.get("login") or "",
            "place": row.get("place"),
            "ratingStars": row.get("ratingStars"),
            "statusName": row.get("statusName"),
            "statusId": row.get("statusId"),
            "statusIcon": row.get("statusIcon"),
            "coins": row.get("coins"),
            "todayWins": row.get("todayWins"),
            "todayGames": row.get("todayGames"),
            "source": row.get("source") or "after_match",
            "ok": bool(row.get("ok")),
            "error": row.get("error"),
            "todayError": row.get("todayError"),
        }
        try:
            await asyncio.to_thread(self._post_profile_to_desk, desk, payload)
        except Exception as exc:
            print(f"[профиль][ERROR] push desk: {exc}")

    @staticmethod
    def _post_profile_to_desk(desk_url: str, payload: dict) -> None:
        import urllib.error
        import urllib.request

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{desk_url}/api/belot-profiles/push",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc.reason if hasattr(exc, "reason") else exc)) from exc

    async def _maybe_leave_for_profile(self, phase: int) -> bool:
        """After match end: leave table, snapshot profile, let client rejoin.

        Returns True if we handled the boundary (caller must not READY).
        """
        if not self.profile_after_match or phase != GAME_ENDED:
            return False
        if self._playing_a_tournament():
            return False
        if self.stop_requested:
            return False
        key = (
            int(getattr(self.client, "sessions_played", 0) or 0),
            int(getattr(self.sync_engine, "hand_id", 0) or 0),
        )
        if self._profile_leave_key == key:
            return True
        self._profile_leave_key = key
        if not self._leave_sent:
            self._leave_sent = True
            print("[Bot] партия окончена — выхожу, сниму рейтинг, потом новый стол.")
            await self.client.leave_table(restart=True)
        await asyncio.sleep(2.5)
        await self._refresh_belot_profile()
        return True

    def _check_room_change(self):
        """Notice that the client joined a different table and start clean.

        Reclaim / OTHER_SESSION reconnect bumps ``sessions_played`` but keeps
        the same ``room_id``. Wiping mid-hand there (pending play, agent
        state, debounce) is what made us play one card then freeze until the
        site bot sat down.
        """
        seq = getattr(self.client, "sessions_played", 0)
        room = getattr(self.client, "room_id", None)
        same_room = (
            bool(room)
            and room == getattr(self, "_bound_room_id", None)
            and self._room_seq
        )
        if seq == self._room_seq and room == getattr(self, "_bound_room_id", None):
            return
        if same_room:
            self._room_seq = seq
            print(f"[Bot] Reconnected to same room {room} (session {seq}); "
                  f"keeping match state.")
            self._table_started = True
            # Allow a fresh READY / deactivate path without wiping the hand.
            self._ready_sent_ts = 0.0
            return
        if self._room_seq:
            print(f"[Bot] New table (session {seq}); clearing per-table "
                  f"state.")
            hist = getattr(self, "history", None)
            if hist is not None and hist.status == "running":
                try:
                    hist.finalize("interrupted", reason="new_table")
                except Exception as exc:
                    print(f"[HISTORY][ERROR] new_table: {exc}")
        self._room_seq = seq
        self._bound_room_id = room
        self._reset_room_state()
        self._agent_reset()
        if getattr(self, "_host_next_table", False):
            # The table we just left was not ours. This one is: we created
            # it on the way back, so players who never ready can be removed
            # instead of taking the whole room down with them.
            self._host_next_table = False
            self.is_host = True
            print("[Bot] This table is ours; a player who does not ready "
                  "will be removed.")
        if getattr(self.client, "resumed_match", False):
            # Back in a match we lost the connection to, not at a new
            # table. Between two of its hands (phase 13) the table looks
            # idle, and a host that thought the match had not started
            # would delete the table after its start grace -- walking out
            # of the very match it just came back to.
            print("[Bot] Resuming a match in progress at this table.")
            self._table_started = True

    async def on_state_update(self, raw_state: dict, my_player_id: str):
        try:
            await self._on_state_update(raw_state, my_player_id)
        except Exception:
            # client.py fires this via asyncio.create_task; an unhandled
            # exception there is swallowed as "Task exception was never
            # retrieved" and the frame is lost without a trace.
            import traceback
            print("[Bot][ERROR] state handler raised:")
            traceback.print_exc()

    def request_stop(self):
        """Finish the current match, then leave. Safe to call more than once."""
        if self.stop_requested:
            return
        self.stop_requested = True
        self.client.stop_requested = True
        self.client.reconnect = False
        print("[Bot] остановка после этой партии; место освободится, "
              "когда партия закончится.")

    def clear_stop(self):
        """Отмена soft-stop со стола — продолжаем искать/играть столы."""
        if not self.stop_requested:
            return
        self.stop_requested = False
        self.client.stop_requested = False
        self.client.reconnect = bool(getattr(self.config, "reconnect", True))
        self._stop_from_desk = False
        print("[Bot] остановку отменили — продолжаю игру.")

    def _poll_stop_file(self):
        """Стол: файл stop → доиграть; go/удалили файл → отмена остановки."""
        path = os.environ.get("BELOT_STOP_FILE") or ""
        if not path:
            return
        p = Path(path)
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
            if text in ("go", "cancel", "continue"):
                if self.stop_requested:
                    self.clear_stop()
                return
            self._stop_from_desk = True
            self.request_stop()
        elif self.stop_requested and getattr(self, "_stop_from_desk", False):
            self.clear_stop()

    def _install_stop_handlers(self):
        """CTRL_BREAK / Ctrl-C — мягкая остановка после партии, не kill."""

        def ask_stop(*_args):
            self.request_stop()

        if os.name == "nt":
            signal.signal(signal.SIGINT, ask_stop)
            signal.signal(signal.SIGBREAK, ask_stop)
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, ask_stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, ask_stop)

    async def _on_state_update(self, raw_state: dict, my_player_id: str):
        self._poll_stop_file()
        self._check_room_change()
        my_pos = self.sync_engine.sync(raw_state, my_player_id)
        if my_pos is None:
            return

        # Hand boundary, detected by the sync engine itself (robust to dropped
        # phase frames). A recurrent agent zeroes its hidden state here, which
        # is what training did at the start of every episode.
        if self.sync_engine.consume_new_hand():
            self._agent_reset()
            self.last_action_turn_id = None
            hist = getattr(self, "history", None)
            if hist is not None:
                try:
                    # Сначала матч (иначе note_hand_start молча отбрасывается).
                    self._history_touch_match(raw_state, my_pos)
                    st = self.sync_engine.state
                    seats = int(getattr(self.sync_engine, "seats", 4) or 4)
                    players = []
                    for seat in range(seats):
                        players.append({
                            "seat": seat,
                            "name": self._seat_label(raw_state, seat, my_pos),
                            "us": seat == my_pos,
                        })
                    our_cards = list(st.hands[my_pos]) if 0 <= my_pos < len(st.hands) else []
                    expected_hand = 8 if seats in (3, 4) else None
                    hand_complete = (
                        expected_hand is None or len(our_cards) >= expected_hand
                    )
                    hist.note_hand_start(
                        hand_id=getattr(self.sync_engine, "hand_id", 0),
                        trump=getattr(st, "trump", None),
                        declarer=getattr(st, "declarer", None),
                        scores=self.sync_engine.match_scores,
                        our_hand=our_cards if hand_complete else None,
                        players=players,
                    )
                    if not hand_complete and our_cards:
                        try:
                            hist.note_analysis({
                                "handId": getattr(self.sync_engine, "hand_id", 0),
                                "partialDeal": True,
                                "cardsSeen": len(our_cards),
                                "note": "неполная выдача — ourHand не зафиксирована как полная",
                            })
                        except Exception:
                            pass
                    elif hand_complete and our_cards:
                        # отдельное событие полной руки после выдачи
                        try:
                            hist.note_analysis({
                                "type": "our_hand_complete",
                                "handId": getattr(self.sync_engine, "hand_id", 0),
                                "ourHand": [int(c) for c in our_cards],
                                "cards": len(our_cards),
                            })
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"[HISTORY][ERROR] hand_start: {exc}")

        if self.audit:
            # The verification layer must NEVER take the bot down: a crash
            # here previously killed the whole task, silently dropping the
            # frame (and any turn it contained).
            try:
                self.recorder.record(raw_state, my_player_id)
                self.auditor.check(self.sync_engine, raw_state)
            except Exception as e:
                print(f"[Audit][ERROR] suppressed: {type(e).__name__}: {e}")

        state = self.sync_engine.state
        phase = raw_state.get("currentPhase", 0)
        active_player = raw_state.get("activePlayer", -1)
        self._history_touch_match(raw_state, my_pos)
        self._announce_table(raw_state, my_pos)
        self._announce_plays(raw_state, my_pos)
        self._emit_live_table(raw_state, my_pos)

        await self._check_play_accepted(my_pos)
        await self._check_bid_accepted(my_pos, raw_state)
        await self._check_seat_autopilot(raw_state, my_pos)
        await self._check_seat_connected(raw_state, my_pos)

        # Declarations exist only once all eight cards are dealt (phase 9+).
        # Every other server field -- trump, declarer, topCard, swapSeven --
        # is stale through the deal, so acting on combinationsCanShow before
        # the second deal risks announcing a combination from the PREVIOUS
        # hand.
        if (self.config.auto_declare and phase >= DEAL_CARDS_2
                and phase not in END_PHASES):
            await self._maybe_declare(raw_state, my_pos)
        if self.config.auto_swap_seven:
            if phase == SWAP_SEVEN:
                await self._maybe_swap_seven(my_pos, raw_state)
            elif phase >= DEAL_CARDS_2:
                self._resolve_swap_attempt(my_pos)

        # 1. Auto-Ready on game transitions.
        #
        # Driven by the server's own answer, not by remembering what we sent.
        # `players[me].ready` is in every frame and is the only thing that
        # actually decides whether the table can start, so asking it is both
        # idempotent and self-correcting: a lost READY is retried, and a READY
        # that landed is never repeated.
        #
        # The previous version deduped on the phase alone and kept the flag
        # across rooms. Every table begins at phase 0, so after the first one
        # the dedup suppressed exactly the READY the next table was waiting
        # for -- silently, since nothing rejected anything. We simply never
        # sent it, and sat in the lobby forever.
        if phase == NOT_STARTED or phase in END_PHASES:
            self._agent_reset()
            if self._playing_a_tournament():
                await self._tournament_boundary(raw_state, my_pos, phase)
                return
            # Playing as a pair: the seats have to be right BEFORE we announce
            # ourselves. The match starts the moment all four players are
            # ready, so a READY sent at the wrong moment is what puts our two
            # accounts on opposite teams -- and nobody can start without us.
            # On our way out: never ready up for ANOTHER match. Only at a match
            # boundary, though -- READY is also what deals the next hand of the
            # current match (phase 13), and withholding it there would stall a
            # match we are trying to finish, until the platform bot took over.
            if self.stop_requested and between_matches(phase):
                return
            if await self._maybe_leave_for_profile(phase):
                return
            if await self._manage_table(raw_state, my_pos):
                return
            # Match finished (phase 14). In create-mode we always leave and
            # open a fresh table: rematch READY often never gets a follow-up
            # frame (opponents gone / host "continue" stuck), which looked
            # like another "слетает" freeze on wire=14.
            if phase == GAME_ENDED and not self._leave_sent:
                seated = sum(
                    1 for p in (raw_state.get("players") or [])
                    if isinstance(p, dict) and p.get("id")
                )
                create_mode = (
                    getattr(self.client, "table_mode", "") == "create"
                    or bool(getattr(self, "_host_next_table", False))
                    or self.is_host
                    or my_pos == 0
                )
                if create_mode or seated <= 1:
                    print("[Bot] Match over — leaving to create the next table "
                          f"(seated={seated}).")
                    self._leave_sent = True
                    self.client.table_mode = "create"
                    self._host_next_table = True
                    try:
                        await self.client.deactivate_bot()
                    except Exception:
                        pass
                    await self.client.leave_table(restart=True)
                    return
            if not self._server_says_ready(raw_state, my_pos):
                await self._send_ready_debounced()
            return

        # A hand is being dealt here, so this table worked: forget the tally of
        # abandoned ones. The cap counts tables that went wrong IN A ROW.
        if self.partner and not self._table_started:
            self._table_started = True
            self._recreates = 0
            self._recreate_capped = False

        # 2. Cut deck phase
        if phase == 2 and active_player == my_pos:
            await self.client.cut_deck(15)
            return

        # 3. Network Action Decision (Bidding or Playing)
        #
        # A seat plays exactly ONE card per trick, and the live gate has to
        # enforce that structurally. belot.md publishes a
        # transient frame in which our card is already on the table and our
        # hand has shrunk, but `activePlayer` has NOT yet moved on (the tell is
        # timeleft dropping to 0). Captured live, 6 ms wide:
        #
        #   activePlayer=3  cards "yEBdel"  no cardPlayed        <- our turn
        #   activePlayer=3  cards "yEBdl"   cardPlayed 'e'  t=0  <- THIS ONE
        #   activePlayer=0  cards "yEBdl"   cardPlayed 'e'       <- moved on
        #
        # `active_player == my_pos` is satisfied on the middle frame, and the
        # debounce key changes in the very same instant
        # (HAND_6_TRICK_0 -> HAND_5_TRICK_1), so neither guard blocks it: the
        # bot dispatches a second card into a trick it has already played
        # into. The server refuses it and _check_play_accepted reports
        # "the server recorded e for our seat instead".
        already_played = any(s_ == my_pos for s_, _ in state.current_trick)
        if phase == PLAY and already_played:
            return                       # our card is already on the table

        if phase in (*BID_PHASES, PLAY) and active_player == my_pos:
            turn_id = (f"PHASE_{phase}_P{my_pos}"
                       f"_HAND_{len(state.hands[my_pos])}"
                       f"_TRICK_{len(state.current_trick)}")
            now = time.monotonic()
            redispatch_after = self.config.redispatch_after_s
            if (self.last_action_turn_id == turn_id
                    and (now - self.last_dispatch_ts) < redispatch_after):
                return
            if self.last_action_turn_id == turn_id:
                print(f"[Bot][WARN] State unchanged {redispatch_after}s after "
                      f"dispatch -- re-dispatching (possible server rejection): "
                      f"{turn_id}")
            self.last_action_turn_id = turn_id
            self.last_dispatch_ts = now

            await self._select_and_execute_action(my_pos, turn_id)

    @staticmethod
    def _table_seats(raw_state):
        """How many seats this table plays to.

        Live frames carry `maxPlayers` (3 here, 4 on the classic table) and
        `gameType` (`GAME_3` / `GAME_4`). A frame that names neither — the
        seat tests — is a four-seat table, which is what those tests build.
        """
        return table_seats(raw_state)

    @staticmethod
    def _four_player_table(raw_state):
        """True only when the server itself says this is a four-player table."""
        if raw_state.get("gameType") == "GAME_4":
            return True
        n = raw_state.get("maxPlayers")
        return (isinstance(n, (int, float)) and not isinstance(n, bool)
                and int(n) == 4)

    def _playing_a_tournament(self):
        config = getattr(self, "config", None)
        return getattr(config, "table_mode", None) == "tournament"

    async def _tournament_boundary(self, raw_state, my_pos, phase):
        """Before a tournament match, between its hands, and after it.

        Two rules differ from an ordinary table. READY goes out whatever stop
        is pending: the match is the draw's, with a partner it gave us, and
        the run ends when we are out of the tournament, not at a match
        boundary (the client handles that). And once the match is over (14)
        there is no next one here -- the site offers only leaving, so we
        leave, and the tournament page says where we play next.
        """
        game_type = raw_state.get("gameType")
        if (game_type and game_type != GAME_TYPE_TOURNAMENT
                and not self._game_type_warned):
            self._game_type_warned = True
            print(f"[Bot][WARN] playing a tournament, but this room is a "
                  f"{game_type} table, not {GAME_TYPE_TOURNAMENT}.")
        if phase == GAME_ENDED:
            if not self._leave_sent:
                self._leave_sent = True
                print("[Bot] Tournament match over; leaving the table, as the "
                      "site's own button does.")
                await self.client.leave_table()
            return
        if not self._server_says_ready(raw_state, my_pos):
            await self._send_ready_debounced()

    def _partner_seat(self, raw_state):
        """Where our other account is sitting, or None if it is not here.

        Matched on the username we were configured with. No other player's
        name is read, compared or recorded.
        """
        if not self.partner:
            return None
        for seat, player in enumerate(raw_state.get("players") or []):
            if (player.get("name") or "").strip().casefold() == self.partner:
                return seat
        return None

    def _seats_are_wrong(self, raw_state, my_pos):
        """True while we are a pair and our partner is not opposite us."""
        if not self.partner:
            return False
        return self._partner_seat(raw_state) != (my_pos + 2) % 4

    def _ready_blocked(self, raw_state, my_pos):
        """Should we withhold READY?

        Playing alone: never -- announce yourself at once, exactly as before.
        This is the single-agent path and nothing above changes it.

        Playing as a pair: ready up only at a FULL table with our partner
        opposite us. Readying earlier is how a fourth player arriving starts a
        match instantly, with whoever happens to be sitting across from us.
        """
        if not self.partner:
            return False
        players = raw_state.get("players") or []
        if sum(1 for p in players if p.get("id")) < 4:
            return True
        return self._partner_seat(raw_state) != (my_pos + 2) % 4

    @staticmethod
    def _rotations_needed(my_pos, partner_seat):
        """How many rotations put our partner opposite us.

        A rotation moves every seat but ours one place forward, so the three
        other chairs are a 3-cycle and the answer is a subtraction rather than
        something to search for. [CONFIRMED live: 1 when the partner sits next
        to us on our left, 2 when on our right, 0 when already opposite.]
        """
        others = [(my_pos + k) % 4 for k in (1, 2, 3)]
        if partner_seat not in others:
            return 0
        target = (my_pos + 2) % 4
        return (others.index(target) - others.index(partner_seat)) % 3

    async def _manage_table(self, raw_state, my_pos):
        """Decide what this table is worth, every idle frame.

        -> True when READY must be withheld, or when we are walking away.

        This runs whether or not the seats are right, because the case it
        exists for is a table that is FULL and correct and still does not
        start: somebody sat down and never readied up. Judging that from
        inside the fix-the-seats branch would never fire, since a correct
        table never enters it.

        Playing alone, a full table that still has not started is not left to
        sit there. Seat 0 -- the creator, and the only seat the site lets
        remove anyone -- kicks the player who never readied. Anyone else
        leaves and makes their own table, and kicks from then on.
        """
        if (not self._playing_a_tournament()
                and self._four_player_table(raw_state)
                and raw_state.get("currentPhase", NOT_STARTED) == NOT_STARTED
                and not self._leave_sent):
            await self._leave_to_host(
                "this is a 4-player table; this build plays tables of three")
            return True

        if not self.partner:
            return await self._unstick_unready(raw_state, my_pos)

        players = raw_state.get("players") or []
        seated = sum(1 for p in players if p.get("id"))
        seats = self._table_seats(raw_state)
        partner_seat = self._partner_seat(raw_state)
        now = time.monotonic()

        # Logged BEFORE anything is decided on it. The probe and the abandon
        # rules used to run first and return, so the layout they acted on was
        # never printed: the log showed a table without our partner, then a
        # deletion for a reason that only makes sense if the partner was there.
        self._log_seats(players, my_pos, partner_seat, seats)

        # Armed the FIRST time this table fills, and never re-armed. Restarting
        # it whenever somebody leaves and is replaced is precisely how a table
        # that churns one seat could keep us waiting for ever.
        if seated >= seats and self._full_since is None:
            self._full_since = now
        if self._room_since is None:
            self._room_since = now

        if self.is_host and not self._table_started:
            # The deliberate experiment, before anything can go wrong on its
            # own: our partner is here and nobody else is yet, so abandoning
            # now proves the whole path -- leave, close, create again, guest
            # finds the new table -- without ejecting a single stranger.
            if (not self._leave_sent and partner_seat is not None
                    and self._leave_probes_done < self.leave_probe):
                self._leave_probes_done += 1
                await self._recreate_table(
                    f"LEAVE PROBE {self._leave_probes_done}/{self.leave_probe}: "
                    f"abandoning this table on purpose")
                return True
            if seated >= seats and partner_seat is None:
                # Every seat taken and none of them ours: this can never become
                # the table we want, and we will not spend a rated hand on a
                # stranger as partner. Wait a few seconds first in case our
                # partner is simply a step behind.
                if now - self._full_since >= self.FULL_WITHOUT_PARTNER_GRACE_S:
                    await self._recreate_table(
                        f"the table filled up without our partner "
                        f"{self.FULL_WITHOUT_PARTNER_GRACE_S:.0f}s ago")
                return True
            if (partner_seat is None
                    and now - self._room_since > self.NO_PARTNER_GIVE_UP_S):
                # Never filled, and our partner never came. Nothing here is
                # going to change on its own.
                await self._recreate_table(
                    f"{self.NO_PARTNER_GIVE_UP_S:.0f}s at this table and our "
                    f"partner has not arrived")
                return True
            if (self._full_since is not None
                    and now - self._full_since > self.TABLE_START_GRACE_S):
                # Prefer removing the player who is actually holding the table
                # up. Deleting it is the fallback for when we cannot tell who
                # that is (or we are not the creator, so the kick would be
                # refused) -- the same give-up this branch had before.
                if (raw_state.get("currentPhase", NOT_STARTED) == NOT_STARTED
                        and self._can_remove(my_pos)):
                    unready = self._unready_seats(raw_state, my_pos)
                    if unready:
                        await self._kick_unready(unready)
                        return True
                await self._recreate_table(
                    f"{self.TABLE_START_GRACE_S:.0f}s since this table filled "
                    f"and still no match -- somebody is not readying up")
                return True

        if self._ready_blocked(raw_state, my_pos) or self._probe_pending(raw_state):
            await self._fix_seats(raw_state, my_pos)
            return True
        return False

    def _unready_due(self, raw_state, my_pos):
        """Seats that have been occupied, and not ready, for the whole grace.

        The clock belongs to the player, not to the foursome. Someone else
        arriving or leaving must not forgive the one person who has been
        sitting there the whole time without readying -- that reset is why a
        churning seat could hold a table forever.
        """
        now = time.monotonic()
        partner = self._partner_seat(raw_state) if self.partner else None
        present = {}
        for seat, player in enumerate(raw_state.get("players") or []):
            pid = player.get("id")
            if not pid or seat == my_pos or seat == partner:
                continue
            if player.get("ready"):
                continue
            present[str(pid)] = seat
        self._unready_since = {
            pid: ts for pid, ts in self._unready_since.items() if pid in present
        }
        due = []
        for pid, seat in present.items():
            since = self._unready_since.get(pid)
            if since is None:
                self._unready_since[pid] = now
                continue
            if now - since > self.TABLE_START_GRACE_S:
                due.append(seat)
        return due

    def _unready_seats(self, raw_state, my_pos):
        """Seats that are occupied and have not readied, other than ours.

        Our partner is never one of them: a pair holds READY on purpose until
        the seats are right, and removing them would be the bot kicking itself.
        """
        partner = self._partner_seat(raw_state) if self.partner else None
        seats = []
        for seat, player in enumerate(raw_state.get("players") or []):
            if seat == my_pos or seat == partner:
                continue
            if player.get("id") and not player.get("ready"):
                seats.append(seat)
        return seats

    def _can_remove(self, my_pos):
        """Can this seat send REMOVE_PLAYER?

        gameplay.js draws the kick control only when our seat is 0, which is
        where the creator sits, and only in phase 0. Never kick from a table
        we merely joined in the lobby — that is someone else's table.
        """
        if my_pos != 0:
            return False
        mode = getattr(self.client, "table_mode", None) or self.config.table_mode
        return bool(self.is_host or mode == "create" or self._host_next_table)

    async def _kick_unready(self, seats):
        """Remove one player who is holding the table up. -> whether we sent."""
        now = time.monotonic()
        for seat in seats:
            if now - self._kick_ts.get(seat, 0.0) < self.KICK_RETRY_S:
                continue
            self._kick_ts[seat] = now
            print(f"[Bot] seat {seat} has not readied up; removing them.")
            await self.client.remove_player(seat)
            return True
        return False

    async def _leave_to_host(self, reason):
        """Leave a table we do not run, and create the next one ourselves.

        Private-squad guests (password + join) must NEVER become hosts: that
        splits the trio across two tables. They leave and look for the host
        again instead.
        """
        mode = getattr(self.client, "table_mode", None) or self.config.table_mode
        private_guest = (
            bool((self.config.table_password or "").strip())
            and mode == "join"
        )
        if private_guest:
            if getattr(self.client, "_rejoin_hint", None) is not None:
                self.client._rejoin_hint = None
            print(f"[Bot] {reason}; private guest — ищу хоста снова, свой стол не открываю.")
            await self._recreate_table(reason, hosting=False)
            return
        self.client.table_mode = "create"
        self._host_next_table = True
        if getattr(self.client, "_rejoin_hint", None) is not None:
            self.client._rejoin_hint = None
        await self._recreate_table(reason, hosting=True)

    async def _unstick_unready(self, raw_state, my_pos):
        """A full table whose match has not started because someone never
        readied. -> True when we are leaving it.

        Only phase 0. Between hands of a match that already started, walking
        out is how an account loses the right to create tables, and the site
        does not offer a kick there either.
        """
        if raw_state.get("currentPhase", NOT_STARTED) != NOT_STARTED:
            self._unready_since = {}
            return False
        if raw_state.get("gameType") == GAME_TYPE_TOURNAMENT:
            return False
        # The clock runs as soon as they sit. The kick itself waits until the
        # table is full: an empty seat is not their fault, a full table that
        # still cannot start is.
        due = self._unready_due(raw_state, my_pos)
        seated = sum(1 for p in (raw_state.get("players") or []) if p.get("id"))
        if not due or seated < self._table_seats(raw_state):
            return False
        if self._can_remove(my_pos):
            await self._kick_unready(due)
            return False
        await self._leave_to_host(
            f"{self.TABLE_START_GRACE_S:.0f}s at a full table and somebody "
            f"is not readying up")
        return True

    def _log_seats(self, players, my_pos, partner_seat, seats=4):
        """Report the layout whenever it changes. -> the layout."""
        layout = self._seat_layout(players, my_pos, partner_seat, seats)
        if layout != self._seat_map:
            self._seat_map = layout
            where = " ".join(f"{s}={r}" for s, r in enumerate(layout))
            print(f"[Bot] seats {where} -- our partner belongs at seat "
                  f"{(my_pos + 2) % 4}.")
        return layout

    def _seat_layout(self, players, my_pos, partner_seat, seats=4):
        """Who sits where, named so that MOVEMENT is visible but people are not.

        Our two accounts appear by role; everyone else gets a letter, kept
        stable for as long as we are at this table. That is enough to see a
        rotation happen -- `1=A 3=B` becoming `1=B 3=A` -- without ever writing
        down another player's name or id.
        """
        layout = []
        for seat in range(seats):
            if seat == my_pos:
                layout.append("us")
            elif seat == partner_seat:
                layout.append("partner")
            elif seat < len(players) and players[seat].get("id"):
                pid = str(players[seat]["id"])
                if pid not in self._seat_labels:
                    self._seat_labels[pid] = chr(ord("A") + len(self._seat_labels))
                layout.append(self._seat_labels[pid])
            else:
                layout.append("empty")
        return tuple(layout)

    def _probe_pending(self, raw_state):
        """Is a deliberate rotation experiment still owed?

        `CHANGE_PLAYERS_POSITION` is the only message we send whose payload and
        effect are unverified, and a table where the seats come out right by
        luck never exercises it. The probe rotates anyway -- correct layout or
        not -- so that what the message does becomes a matter of record.
        """
        return (self.is_host and self._probe_done < self.rotation_probe
                and self._partner_seat(raw_state) is not None)

    async def _fix_seats(self, raw_state, my_pos):
        """Hold READY, and -- if we made this table -- rotate the seats.

        Rotation is the creator's to do, so the guest simply waits.
        """
        players = raw_state.get("players") or []
        partner_seat = self._partner_seat(raw_state)
        seated = sum(1 for p in players if p.get("id"))

        now = time.monotonic()
        if not self.is_host:
            return                      # the guest can only wait

        # The experiment comes first, and ignores whether the seats are right.
        if partner_seat is not None and self._probe_done < self.rotation_probe:
            if now - self._rotate_ts < self.ROTATE_PROBE_GAP_S:
                return
            self._rotate_ts = now
            self._probe_done += 1
            where = " ".join(f"{s}={r}" for s, r in enumerate(self._seat_map or ()))
            print(f"[Bot][PROBE] rotation {self._probe_done}/"
                  f"{self.rotation_probe} sent, from {where} -- the next "
                  f"'seats' line is what it did.")
            await self.client.change_players_position()
            return

        if partner_seat is None or seated < self._table_seats(raw_state):
            # Nothing to rotate yet: an empty seat will be filled by whoever
            # sits down next, and that changes the arrangement anyway.
            return
        if self._rotations >= self.MAX_ROTATIONS:
            # Two rotations always suffice, so a third means the seat rule is
            # not behaving as measured. Take a different table rather than
            # shuffling this one on a broken assumption.
            await self._recreate_table(
                f"{self._rotations} rotations and our partner is still not "
                f"opposite, which should be impossible")
            return
        if now - self._rotate_ts < self.ROTATE_RETRY_S:
            return

        self._rotate_ts = now
        self._rotations += 1
        # Every rotation ejects the other three players and makes them rejoin,
        # and some humans simply do not come back -- so say how many are left
        # and never send more than the arithmetic calls for.
        needed = self._rotations_needed(my_pos, partner_seat)
        print(f"[Bot] rotating the other seats ({self._rotations}/"
              f"{self.MAX_ROTATIONS}); {needed} rotation(s) should do it.")
        await self.client.change_players_position()

    async def _recreate_table(self, reason, hosting=False):
        """Abandon this table and make another one.

        As the creator, leaving DELETES the table and ejects everyone sitting
        at it -- so at most one per table, and a hard cap per run. If five
        tables in a row go wrong, the problem is not the tables, and churning
        the lobby is worse than waiting.

        `hosting` is a guest walking out of someone else's table to create
        its own. That leave does not delete their table; it only frees our seat.
        """
        if self._leave_sent:
            return
        if self._recreates >= self.MAX_RECREATES:
            if not self._recreate_capped:
                self._recreate_capped = True
                print(f"[Bot][WARN] {reason} -- but {self._recreates} tables "
                      f"have already been abandoned. Waiting here instead of "
                      f"churning the lobby.")
            return

        self._leave_sent = True
        self._recreates += 1
        doing = ("leaving it to make our own" if hosting
                 else "deleting this table and making another")
        print(f"[Bot] {reason}; {doing} "
              f"({self._recreates}/{self.MAX_RECREATES}).")
        await self.client.leave_table()

    @staticmethod
    def _server_says_ready(raw_state, my_pos):
        players = raw_state.get("players", [])
        if my_pos >= len(players):
            return False
        return bool(players[my_pos].get("ready"))

    async def _send_ready_debounced(self):
        """Announce readiness, at most once a second.

        The server needs a frame or two to reflect it, and several arrive in
        that window. Retrying is correct -- flooding is not.
        """
        now = time.monotonic()
        if now - self._ready_sent_ts < self.READY_RESEND_S:
            return
        self._ready_sent_ts = now
        await self.client.send_ready(True)

    async def _check_bid_accepted(self, my_pos, raw_state=None):
        """Подтверждение торговли только серверным событием (не сменой раздачи)."""
        if self._pending_bid is None:
            return
        hand_id, action = self._pending_bid
        state = self.sync_engine.state
        # смена раздачи ≠ confirm
        if hand_id != self.sync_engine.hand_id:
            self._pending_bid = None
            pending = getattr(self, "_history_pending", None)
            if pending is not None and str(pending.get("phase") or "").upper() in (
                "BIDDING", "BID", ""
            ):
                pending["confirm_status"] = "unresolved"
                pending["confirmed"] = False
                pending["server_confirmed"] = False
                hist = getattr(self, "history", None)
                if hist is not None:
                    try:
                        hist.note_decision(pending)
                    except Exception:
                        pass
                self._history_pending = None
            return

        accepted = False
        # ACCEPT / suit bid: сервер выставил козырь и declarer = мы
        if state.trump is not None and state.declarer == my_pos:
            if action == ACTION_ACCEPT or action in SUIT_ACTIONS:
                accepted = True
        # PASS: ход ушёл с нас, козырь ещё не наш / фаза сменилась без нашего заказа
        if action == ACTION_PASS:
            if state.phase != "BIDDING":
                # торги закончились без нас как declarer
                if state.declarer != my_pos:
                    accepted = True
            elif getattr(state, "active_player", None) is not None and state.active_player != my_pos:
                # ход перешёл — пас принят (эвристика; не reject)
                accepted = True

        if not accepted:
            return

        self._pending_bid = None
        pending = getattr(self, "_history_pending", None)
        hist = getattr(self, "history", None)
        if pending is not None and hist is not None:
            try:
                pending["confirmed_action"] = int(action)
                pending["confirmed"] = True
                pending["server_confirmed"] = True
                pending["confirm_status"] = "server_confirmed"
                pending["count_decision"] = True
                hist.note_decision(pending)
            except Exception as exc:
                print(f"[HISTORY][ERROR] bid confirm: {exc}")
            self._history_pending = None
        if self.journal is not None and self._journal_decision_id:
            try:
                self.journal.record_confirm(self._journal_decision_id, action)
            except Exception:
                pass
            self._journal_decision_id = None

    async def _check_play_accepted(self, my_pos):
        """Did the server actually take the card we sent?

        A live session showed the bot dispatching the same card in six
        consecutive tricks: every play was being ignored while the tricks
        resolved around it, so the model was effectively not playing at all.
        Nothing detected that. The card leaving our hand is the server's own
        confirmation; anything else is a rejection worth shouting about.
        """
        if self._pending_play is None:
            return
        hand_id, trick_no, card = self._pending_play
        state = self.sync_engine.state
        # Смена раздачи сама по себе ≠ подтверждение хода
        if hand_id != self.sync_engine.hand_id:
            self._pending_play = None
            pending = getattr(self, "_history_pending", None)
            if pending is not None:
                pending["confirm_status"] = "unresolved"
                pending["confirmed"] = False
                pending["server_confirmed"] = False
                hist = getattr(self, "history", None)
                if hist is not None:
                    try:
                        hist.note_decision(pending)
                    except Exception:
                        pass
                self._history_pending = None
            return

        card_still_in_hand = card in state.hands[my_pos]
        played_by_us = next((c for s_, c in state.current_trick if s_ == my_pos), None)
        # Подтверждение: карта на столе от нас ИЛИ ушла из руки в той же взятке
        accepted = (
            (played_by_us is not None and int(played_by_us) == int(card))
            or (not card_still_in_hand and played_by_us is None
                and state.tricks_played == trick_no)
            or (not card_still_in_hand and state.tricks_played > trick_no
                and any(int(c) == int(card) for s, c in (getattr(state, "last_trick", None) or [])
                        if int(s) == int(my_pos)))
        )
        if accepted:
            self._pending_play = None
            self._pending_play_ts = 0.0
            self._rejected_plays = 0
            pending = getattr(self, "_history_pending", None)
            hist = getattr(self, "history", None)
            if pending is not None and hist is not None:
                try:
                    pending["confirmed_action"] = int(card)
                    pending["confirmed"] = True
                    pending["server_confirmed"] = True
                    pending["confirm_status"] = "server_confirmed"
                    pending["count_decision"] = True
                    hist.note_decision(pending)
                except Exception as exc:
                    print(f"[HISTORY][ERROR] server confirm: {exc}")
                self._history_pending = None
            if self.journal is not None and self._journal_decision_id:
                try:
                    self.journal.record_confirm(self._journal_decision_id, card)
                except Exception:
                    pass
                self._journal_decision_id = None
            return

        moved_on = state.tricks_played > trick_no
        if card_still_in_hand and played_by_us is None and not moved_on:
            # Still waiting for the server. Cap the wait so a swallowed
            # PLAY_CARD cannot freeze us on this trick forever.
            sent_at = float(getattr(self, "_pending_play_ts", 0.0) or 0.0)
            if sent_at and (time.monotonic() - sent_at) < 8.0:
                return
            print(f"[Bot][WARN] play {ID_TO_ASCII.get(card)} still in hand "
                  f"after 8s with no ack — clearing pending for retry")
            self._pending_play = None
            self._pending_play_ts = 0.0
            self.last_action_turn_id = None
            return

        self._pending_play = None
        self._pending_play_ts = 0.0
        self._rejected_plays += 1
        pending = getattr(self, "_history_pending", None)
        hist = getattr(self, "history", None)
        if pending is not None and hist is not None:
            try:
                pending["rejected"] = True
                pending["confirmed"] = False
                pending["server_confirmed"] = False
                pending["confirm_status"] = "rejected"
                if played_by_us is not None:
                    pending["confirmed_action"] = int(played_by_us)
                hist.note_decision(pending)
            except Exception as exc:
                print(f"[HISTORY][ERROR] reject: {exc}")
            self._history_pending = None
        if self.journal is not None and self._journal_decision_id:
            try:
                self.journal.record_reject(self._journal_decision_id)
            except Exception:
                pass
            self._journal_decision_id = None
        if played_by_us is not None and played_by_us != card:
            detail = (f"the server recorded {ID_TO_ASCII.get(played_by_us)} "
                      f"for our seat instead")
        else:
            detail = "the trick moved on without it"
        cause = (" (expected: the platform bot holds our seat)"
                 if self.seat_bot_controlled else
                 " -- if this repeats, our turn probably timed out and the "
                 "platform bot has taken the seat")
        print(f"[Bot][ERROR] play {ID_TO_ASCII.get(card)} was NOT accepted: "
              f"{detail}{cause}. (rejected plays this session: "
              f"{self._rejected_plays})")

    async def _check_seat_connected(self, raw_state, my_pos):
        """If the server marks us disconnected, nudge BOT_ACTIVATION only.

        A single ``connected=false`` frame is common mid-trick (seen live:
        false for <1s then true again). Reclaiming Colyseus while STATE
        frames are still arriving drops our own socket — that is what
        "слетает" looks like. Sticky false → deactivate only; a truly dead
        wire is the silence watchdog's job (no frames → reclaim).
        """
        players = raw_state.get("players") or []
        if my_pos >= len(players):
            return
        connected = players[my_pos].get("connected")
        now = time.monotonic()
        if connected is not False:
            self._disconnect_since = 0.0
            return
        since = float(getattr(self, "_disconnect_since", 0.0) or 0.0)
        if not since:
            self._disconnect_since = now
            return
        if (now - since) < 3.0:
            return
        last = float(getattr(self, "_disconnect_nudge_ts", 0.0) or 0.0)
        if last and (now - last) < 20.0:
            return
        self._disconnect_nudge_ts = now
        phase = raw_state.get("currentPhase")
        print("=" * 72)
        print(f"[Bot][DISCONNECT] connected=false for {now - since:.1f}s "
              f"(phase {phase}) — BOT_ACTIVATION deactivate only "
              f"(no Colyseus reclaim while frames flow).")
        print("=" * 72)
        try:
            await self.client.deactivate_bot()
        except Exception as exc:
            print(f"[Bot][DISCONNECT] deactivate failed: {exc}")

    async def _check_seat_autopilot(self, raw_state, my_pos):
        """Track the platform bot taking our seat, and reclaim it.

        belot.md hands a seat to its own bot once a turn times out, and from
        then on our PLAY_CARD messages are ignored until we send
        BOT_ACTIVATION deactivate again (same as on join).
        """
        players = raw_state.get("players", [])
        if my_pos >= len(players):
            return
        flagged = bool(players[my_pos].get("bot"))
        if flagged == self.seat_bot_controlled:
            return
        self.seat_bot_controlled = flagged
        rnd = raw_state.get("round")
        phase = raw_state.get("currentPhase")
        if flagged:
            self._takeover_frame = (rnd, phase)
            print("=" * 72)
            print(f"[Bot][SEAT LOST] The platform bot has taken our seat "
                  f"(round {rnd}, phase {phase}).")
            print("               Sending BOT_ACTIVATION deactivate to "
                  "reclaim.")
            print("=" * 72)
            try:
                await self.client.deactivate_bot()
            except Exception as exc:
                print(f"[Bot][SEAT LOST] deactivate failed: {exc}")
            # Do NOT reclaim_connection here: bouncing mid-trick on a transient
            # bot flag drops us worse than deactivate alone. Sticky
            # connected=false still triggers reclaim via _check_seat_connected.
        else:
            print(f"[Bot][SEAT REGAINED] The server no longer flags our seat "
                  f"as bot-controlled (round {rnd}, phase {phase}); we were "
                  f"out from {self._takeover_frame}.")

    async def _maybe_declare(self, raw_state, my_pos):
        """Announce the point combinations the server offers us. Codes are
        "<type><highest_card>", pipe-separated. Combination points land in
        roundTotals.c and never in .p, so declaring cannot perturb any
        observation feature -- it is free match score."""
        players = raw_state.get("players", [])
        if my_pos >= len(players):
            return
        offer = players[my_pos].get("combinationsCanShow") or ""
        for value in combo.declarable(offer, ASCII_TO_ID,
                                      include_win_all=self.config.declare_win_all,
                                      include_four_sevens=self.config.declare_four_sevens,
                                      include_four_eights=self.config.declare_four_eights):
            key = (self.sync_engine.hand_id, value)
            if key in self._declared:
                continue
            self._declared.add(key)
            cards, _ = combo.decode_field(value, ASCII_TO_ID)
            print(f"[Bot Action] -> SHOW COMBINATION: {value} "
                  f"({combo.describe(value, ASCII_TO_ID)}"
                  + (f", cards {[ID_TO_ASCII[c] for c in cards]}" if cards else "")
                  + ")")
            await self.client.show_combination(value)

    async def _maybe_swap_seven(self, my_pos, raw_state):
        """Phase 8: trade our 7 of trump for the face-up card.

        Preconditions, all deduced LOCALLY -- the server sends no "you may
        swap" event; it just opens phase 8 after every round-1 accept and
        waits on a timeout:
          1. phase == 8. Round-2 picks go 7 -> 9 and forced-trump ("BIZON")
             hands go 5 -> 9, so both skip the window with no special case.
          2. trump == the face-up suit (implied by 1, asserted anyway).
          3. we hold the 7 of trump -- and it must be among the FIRST FIVE
             cards, since the rest are dealt at phase 9, after the window.
          4. the face-up card is not itself that 7.
          5. the face-up card is not going to us or our partner: swapping
             inside our own team just shuffles a good trump and the 7 around
             for nothing while announcing that we hold the 7.

        Exactly one message per window, payload {} as PASS sends. Whether it
        worked is read back from the state, not assumed.
        """
        state = self.sync_engine.state
        hand_id = self.sync_engine.hand_id
        if state.trump is None or state.face_up_card is None:
            return
        if state.trump != state.face_up_card // 8:      # not a round-1 accept
            return
        seven = state.trump * 8                       # rank 0 == the 7

        if self._swap_hand != hand_id:              # entering a new window
            self._swap_hand = hand_id
            self._swap_sent = False
            self._swap_skip = False

        # Success is visible in the state: swapSeven carries our seat and the
        # 7 leaves our hand. Report as soon as either shows up.
        if self._swap_sent:
            if raw_state.get("swapSeven", -1) == my_pos or seven not in state.hands[my_pos]:
                if not self._swap_skip:
                    self._swap_skip = True          # nothing further to do
                    print("[Bot] SWAP SEVEN confirmed: we now hold "
                          f"{ID_TO_ASCII[state.face_up_card]}.")
            return

        if self._swap_skip or seven not in state.hands[my_pos]:
            return
        if state.face_up_card == seven:
            return

        recipient = self.sync_engine._natural_face_up_recipient()
        if recipient is not None and (recipient - my_pos) % 2 == 0:
            who = "us" if recipient == my_pos else f"our partner (seat {recipient})"
            print(f"[Bot] holding the 7 of trump, but the face-up card goes "
                  f"to {who} -- not swapping.")
            self._swap_skip = True
            return

        self._swap_sent = True
        print(f"[Bot Action] -> SWAP SEVEN ({ID_TO_ASCII[seven]} for "
              f"{ID_TO_ASCII[state.face_up_card]})")
        await self.client.swap_seven()

    def _resolve_swap_attempt(self, my_pos):
        """Window closed: did the swap actually happen?"""
        if not self._swap_sent:
            return
        self._swap_sent = False
        state = self.sync_engine.state
        seven = state.trump * 8 if state.trump is not None else None
        if seven is None:
            return
        if seven in state.hands[my_pos]:
            print(f"[Bot][WARN] SWAP SEVEN had no effect -- still holding "
                  f"{ID_TO_ASCII[seven]}. Either the payload shape is wrong "
                  f"(capture the outgoing frame from the web client) or the "
                  f"server declined it.")
        elif not self._swap_skip:
            print("[Bot] SWAP SEVEN succeeded (detected after the window).")

    async def on_combination(self, seat, value):
        """SHOW_COMBINATION broadcast -> pin those cards in the belief state.
        The state's per-player `combinations` field carries the same data, so
        this is a redundancy against dropped messages, not the only path."""
        self.sync_engine.apply_combination(seat, value)

    async def _select_and_execute_action(self, my_pos: int, turn_id: str):
        """Ask the agent for one action and put it on the wire.

        One decision per turn. A re-dispatch replays from the SAME pre-turn
        agent state (so a recurrent policy advances once per game decision, as
        it did in training) and blocks every action the server already refused.
        """
        state = self.sync_engine.state

        if self._turn_decision is not None and self._turn_decision[0] == turn_id:
            _, snapshot, refused = self._turn_decision
            self._agent_restore(snapshot)          # replay, don't chain
        else:
            snapshot, refused = self._agent_snapshot(), set()
            self._turn_decision = (turn_id, snapshot, refused)

        legal_mask = state.get_legal_actions().astype(np.int8)
        if not legal_mask.any():
            print(f"[Bot][WARN] empty legal mask on our turn ({turn_id}); "
                  f"nothing dispatched -- the turn will time out unless a "
                  f"newer frame arrives")
            if self.journal is not None:
                self.journal.note_empty_mask(
                    state=state, seat=my_pos,
                    reason=f"empty legal mask ({turn_id})",
                )
            self.last_action_turn_id = None      # allow an immediate retry
            return

        for a in refused:
            legal_mask[a] = 0
        if not legal_mask.any():
            print("[Bot][WARN] every legal action has already been refused "
                  "this turn; giving up rather than looping")
            return

        decision = getattr(self.agent, "last_decision", None)
        if self.journal is not None:
            from .learning.journal import snapshot_state
            self.journal.set_hand(str(getattr(self.sync_engine, "hand_id", "")))
            self._journal_decision_id = self.journal.begin_decision(
                state=state,
                seat=my_pos,
                match_scores=self.sync_engine.match_scores,
                legal_mask=legal_mask,
                source="our_agent",
                agent_name=getattr(self.agent, "name", type(self.agent).__name__),
                model_version=getattr(self.agent, "model_version", None) or "",
                mask_origin="state.get_legal_actions+refused",
                game_mode=f"{int(getattr(state, 'num_players', 4) or 4)}p",
                platform_bot_control=bool(getattr(self, "seat_bot_controlled", False)),
            )
            self._history_pending = {
                "decision_id": self._journal_decision_id,
                "hand_id": str(getattr(self.sync_engine, "hand_id", "")),
                "phase": getattr(state, "phase", None),
                "seat": my_pos,
                "source": "our_agent",
                "agent": getattr(self.agent, "name", type(self.agent).__name__),
                "rule_id": (decision or {}).get("rule_id") if isinstance(decision, dict) else None,
                "reason": (decision or {}).get("reason") if isinstance(decision, dict) else None,
                "legal_actions": [int(i) for i in np.flatnonzero(legal_mask)],
                "state": snapshot_state(state, my_pos),
                "match_scores_before": list(self.sync_engine.match_scores or []),
            }

        t0 = time.monotonic()
        action = self.agent.act(state, my_pos, self.sync_engine.match_scores,
                                legal_mask)
        compute_ms = (time.monotonic() - t0) * 1000.0
        decision = getattr(self.agent, "last_decision", None) or decision

        if not legal_mask[action]:
            # An agent that ignores the mask would get the move refused and
            # eventually lose the seat to the platform bot. Say so loudly
            # rather than letting it look like a server problem.
            print(f"[Bot][CRITICAL] agent returned illegal action {action} "
                  f"({actions.describe(action, ID_TO_ASCII)}); not dispatching")
            if self.journal is not None and self._journal_decision_id:
                self.journal.record_choice(
                    self._journal_decision_id, action, compute_ms=compute_ms)
                self.journal.record_reject(self._journal_decision_id)
            if self._history_pending is not None:
                self._history_pending["intent_action"] = int(action)
                self._history_pending["chosen_action"] = int(action)
                self._history_pending["rejected"] = True
                self._history_pending["confirmed"] = False
                hist = getattr(self, "history", None)
                if hist is not None:
                    try:
                        hist.note_decision(self._history_pending)
                    except Exception as exc:
                        print(f"[HISTORY][ERROR] decision reject: {exc}")
                self._history_pending = None
            return

        if self.journal is not None and self._journal_decision_id:
            self.journal.record_choice(
                self._journal_decision_id, action, compute_ms=compute_ms)
            # НЕ record_confirm до сервера (ТЗ independent review §4).
            if hasattr(self.journal, "record_sent"):
                self.journal.record_sent(self._journal_decision_id, action)
        if self._history_pending is not None:
            if isinstance(decision, dict):
                self._history_pending["rule_id"] = decision.get("rule_id")
                self._history_pending["reason"] = decision.get("reason")
            self._history_pending["intent_action"] = int(action)
            self._history_pending["chosen_action"] = int(action)
            # НЕ ставим confirmed=True до сервера (ТЗ §5)
            self._history_pending["confirmed"] = False
            self._history_pending["server_confirmed"] = False
            self._history_pending["confirm_status"] = "sent"
            self._history_pending["sent"] = True
            self._history_pending["send_attempts"] = int(
                self._history_pending.get("send_attempts") or 0
            ) + 1
            hist = getattr(self, "history", None)
            if hist is not None:
                try:
                    hist.note_decision(self._history_pending)
                except Exception as exc:
                    print(f"[HISTORY][ERROR] decision: {exc}")
            # pending держим до серверного подтверждения / отказа

        refused.add(action)
        await self._dispatch_action(action, state)

    def _tag(self):
        """Prefix for action lines so a log read later is never ambiguous."""
        return "[seat bot-controlled] " if self.seat_bot_controlled else ""

    async def _dispatch_action(self, action: int, state):
        """Translate an action index into the server message that performs it."""
        if state.phase == "BIDDING":
            self._pending_bid = (self.sync_engine.hand_id, int(action))
            if action == ACTION_PASS:
                print(f"{self._tag()}[Bot Action] -> PASS")
                await self.client.pass_turn()

            elif action == ACTION_ACCEPT:
                if state.face_up_suit is None:
                    # Transient race: topCard not yet observed this hand.
                    # ACTION_ACCEPT is only legal in round 1, where passing is
                    # always legal, so this fallback can never be illegal.
                    print("[Bot][WARN] face_up_suit unknown; passing defensively")
                    await self.client.pass_turn()
                else:
                    colyseus_suit = state.face_up_suit + 1
                    print(f"{self._tag()}[Bot Action] -> ACCEPT FACE-UP "
                          f"(Suit: {colyseus_suit})")
                    await self.client.bid_trump(colyseus_suit)

            elif action in SUIT_ACTIONS:
                suit = suit_of(action)
                if state.face_up_suit is None:
                    # get_legal_actions() compares `suit != face_up_suit`; with
                    # None that is True for ALL FOUR suits, so the mask can
                    # offer the flipped suit -- an illegal round-2 bid. The
                    # server refuses it, the turn times out, and the platform
                    # bot takes the seat.
                    if state.get_legal_actions()[ACTION_PASS]:
                        print("[Bot][CRITICAL] face_up_suit unknown in round 2; "
                              "the suit mask is unreliable -- passing instead")
                        await self.client.pass_turn()
                        return
                    print("[Bot][CRITICAL] face_up_suit unknown and passing is "
                          f"illegal (we are the dealer); bidding suit {suit} "
                          "blind -- this may be refused")
                elif suit == state.face_up_suit:
                    print(f"[Bot][CRITICAL] refusing to bid the face-up suit "
                          f"{suit} in round 2 (illegal); passing instead")
                    if state.get_legal_actions()[ACTION_PASS]:
                        await self.client.pass_turn()
                        return
                colyseus_suit = suit + 1
                print(f"{self._tag()}[Bot Action] -> CHOOSE SUIT ({colyseus_suit})")
                await self.client.bid_trump(colyseus_suit)

        elif state.phase == "PLAYING":
            if action in CARD_ACTIONS:
                card_char = ID_TO_ASCII[action]
                print(f"{self._tag()}[Bot Action] -> PLAY CARD: "
                      f"{CHAR_TO_CARD[card_char]} ({card_char})")
                self._pending_play = (self.sync_engine.hand_id,
                                      state.tricks_played, action)
                self._pending_play_ts = time.monotonic()
                await self.client.play_card_char(card_char)

    async def on_server_message(self, msg_type, data):
        """Broadcasts that carry belief information the state frames do not.

        SHOW_COMBINATION and SWAP_SEVEN each pin cards into the belief state.
        The per-player `combinations` / `swapSeven` state fields carry the same
        facts, so these are redundancy against a dropped message rather than
        the only path — but they arrive first, and they are unambiguous.

        This is deliberately NOT gated on audit mode: the auditor's bookkeeping
        is, the belief updates are not.
        """
        try:
            blob = json.dumps(data)[:300]
        except Exception:
            blob = repr(data)[:300]
        print(f"[MSG] {msg_type}: {blob}")
        try:
            if msg_type == "SHOW_COMBINATION" and isinstance(data, dict):
                if self.audit:
                    self.auditor.note_combination(data.get("who"),
                                                  data.get("value"), "message")
                await self.on_combination(data.get("who"), data.get("value"))
            elif msg_type in ("LESS_THAN_14", "FOUR_OF_SEVEN", "FOUR_OF_EIGHT",
                              "WIN_ALL_HANDS", "SURRENDER_BT", "BIZON"):
                if self.audit:
                    self.auditor.note_special(msg_type, data)
            elif msg_type == "SWAP_SEVEN" and isinstance(data, dict):
                who = data.get("who")
                seven = ASCII_TO_ID.get(data.get("swappedCard"))
                top = ASCII_TO_ID.get(data.get("topCard"))
                self.sync_engine.apply_seven_swap(who, seven, top)
                if who == self.sync_engine.my_pos:
                    print("[Bot] server broadcast confirms OUR swap")
        except Exception as e:
            print(f"[MSG][ERROR] suppressed: {type(e).__name__}: {e}")

    async def run(self):
        self._install_stop_handlers()
        try:
            await self.client.connect(
                on_state_callback=self.on_state_update,
                on_message_callback=self.on_server_message,
            )
        finally:
            self.client.close()
            if getattr(self, "journal", None) is not None:
                self.journal.close()
            hist = getattr(self, "history", None)
            if hist is not None and hist.status == "running":
                try:
                    hist.finalize("interrupted", reason="process_exit")
                except Exception as exc:
                    print(f"[HISTORY][ERROR] exit: {exc}")