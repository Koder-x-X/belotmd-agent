"""Поматчевая история одного бота: отдельный JSONL на матч, без чужих закрытых рук."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

HISTORY_FORMAT = "2"
RECORDER_VERSION = "2.1.0"
AGENT_HISTORY_VERSION = "1.0.0"
SCHEMA_VERSION = "history-2.1"


def stable_bot_id(login: str) -> str:
    key = str(login or "").strip().casefold() or "unknown"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"bot_{digest}"


def _safe_jsonl_append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def read_jsonl_tolerant(path: Path) -> list[dict]:
    """Читает JSONL; битая последняя строка пропускается, остальное сохраняется."""
    if not path.is_file():
        return []
    rows = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


class GameHistoryRecorder:
    """Пишет только свой матч. Ошибка записи не должна валить бота."""

    def __init__(
        self,
        *,
        root: Path | str,
        bot_id: str,
        login: str = "",
        agent: str = "",
        agent_version: str = "",
        model_version: str = "",
        variant: str = "3",
        points: str = "",
    ):
        self.root = Path(root)
        self.bot_id = bot_id
        self.login = login
        self.agent = agent
        self.agent_version = agent_version or AGENT_HISTORY_VERSION
        self.model_version = model_version or ""
        self.variant = str(variant or "3")
        self.points = str(points or "")
        self.bot_dir = self.root / "bots" / self.bot_id
        self.bot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.game_id: Optional[str] = None
        self.path: Optional[Path] = None
        self.status = "idle"
        self.started_at: Optional[float] = None
        self.match_key: Optional[str] = None
        self.hands = 0
        self.decisions = 0
        self.seat: Optional[int] = None
        self.result: Optional[str] = None
        self.scores: list = []
        self._meta: dict = {}
        self._process_started_at = time.time()

    def _emit_desk(self, op: str, extra: dict | None = None) -> None:
        payload = {
            "op": op,
            "botId": self.bot_id,
            "login": self.login,
            "gameId": self.game_id,
            "status": self.status,
            "path": str(self.path) if self.path else None,
            "agent": self.agent,
            "agentVersion": self.agent_version,
            "variant": self.variant,
            "points": self.points,
            "hands": self.hands,
            "decisions": self.decisions,
            "startedAt": self.started_at,
            "result": self.result,
            "scores": self.scores,
            "matchKey": self.match_key,
            "seat": self.seat,
        }
        if extra:
            payload.update(extra)
        try:
            print(f"[HISTORY]{json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)
        except Exception:
            pass

    def _write(self, event: dict) -> None:
        if not self.path:
            return
        event = dict(event)
        event.setdefault("t", time.time())
        event.setdefault("gameId", self.game_id)
        try:
            _safe_jsonl_append(self.path, event)
        except OSError as exc:
            try:
                print(f"[HISTORY][ERROR] write failed: {exc}", flush=True)
            except Exception:
                pass

    def _update_local_index(self) -> None:
        index_path = self.bot_dir / "games.json"
        try:
            data = {}
            if index_path.is_file():
                data = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
            games = data.get("games")
            if not isinstance(games, dict):
                games = {}
            if self.game_id:
                games[self.game_id] = {
                    "gameId": self.game_id,
                    "botId": self.bot_id,
                    "login": self.login,
                    "file": self.path.name if self.path else None,
                    "status": self.status,
                    "agent": self.agent,
                    "agentVersion": self.agent_version,
                    "modelVersion": self.model_version or None,
                    "variant": self.variant,
                    "points": self.points,
                    "startedAt": self.started_at,
                    "endedAt": self._meta.get("endedAt"),
                    "hands": self.hands,
                    "decisions": self.decisions,
                    "result": self.result,
                    "scores": self.scores,
                    "matchKey": self.match_key,
                    "seat": self.seat,
                    "bytes": self.path.stat().st_size if self.path and self.path.is_file() else 0,
                    "packedIn": self._meta.get("packedIn"),
                    "reconstructionStatus": self._meta.get("reconstructionStatus"),
                    "analysisStatus": self._meta.get("analysisStatus"),
                }
            data["games"] = games
            data["updatedAt"] = time.time()
            _atomic_write_json(index_path, data)
        except Exception as exc:
            try:
                print(f"[HISTORY][ERROR] index: {exc}", flush=True)
            except Exception:
                pass

    def ensure_match(
        self,
        *,
        match_key: str,
        seat: int,
        seats: int,
        room_id=None,
        table_id=None,
    ) -> None:
        with self._lock:
            if self.game_id and self.match_key == match_key and self.status in ("running", "interrupted"):
                if self.status == "interrupted":
                    self.status = "running"
                    self._write({"type": "reconnect", "matchKey": match_key, "roomId": room_id, "tableId": table_id})
                    self._update_local_index()
                    self._emit_desk("upsert")
                return
            if self.game_id and self.status == "running":
                self._finalize_locked("interrupted", reason="new_match")
            self.game_id = f"g_{uuid.uuid4().hex}"
            self.path = self.bot_dir / f"game_{self.game_id}.jsonl"
            self.status = "running"
            self.started_at = time.time()
            self.match_key = match_key
            self.hands = 0
            self.decisions = 0
            self.seat = int(seat)
            self.result = None
            self.scores = []
            self._meta = {"endedAt": None, "packedIn": None}
            # matchKey = подсказка комнаты; уникальность участия — gameId.
            # room может переиспользоваться → uncertainty, если нет table+room+time.
            header = {
                "type": "meta",
                "format": HISTORY_FORMAT,
                "schemaVersion": SCHEMA_VERSION,
                "recorderVersion": RECORDER_VERSION,
                "gameId": self.game_id,
                "botId": self.bot_id,
                "login": self.login,
                "agent": self.agent,
                "agentVersion": self.agent_version,
                "modelVersion": self.model_version or None,
                "variant": self.variant,
                "points": self.points,
                "seats": int(seats),
                "seat": int(seat),
                "matchKey": match_key,
                "matchKeyNote": "tableId:roomId — не уникальный матч при reuse комнаты",
                "participationId": self.game_id,
                "roomId": room_id,
                "tableId": table_id,
                "startedAt": self.started_at,
                "processStartedAt": getattr(self, "_process_started_at", None) or self.started_at,
                "buildId": os.environ.get("BELOT_BUILD_ID") or RECORDER_VERSION,
                "status": "running",
            }
            self._write(header)
            self._update_local_index()
            self._emit_desk("upsert")

    def note_hand_start(
        self,
        *,
        hand_id,
        trump=None,
        declarer=None,
        scores=None,
        our_hand=None,
        players=None,
    ) -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            self.hands += 1
            payload = {
                "type": "hand_start",
                "handId": hand_id,
                "handNo": self.hands,
                "trump": trump,
                "declarer": declarer,
                "scores": list(scores or []),
            }
            if our_hand is not None:
                payload["ourHand"] = [int(c) for c in our_hand]
            if players is not None:
                payload["players"] = players
            self._write(payload)
            self._update_local_index()
            self._emit_desk("upsert")

    def note_analysis(self, payload: dict) -> None:
        """Лёгкая пометка в файл (статус реконструкции/анализа раздачи)."""
        with self._lock:
            if not self.game_id:
                return
            data = dict(payload)
            data["type"] = "analysis_note"
            self._write(data)
            if data.get("reconstructionStatus"):
                self._meta["reconstructionStatus"] = data.get("reconstructionStatus")
            if data.get("analysisStatus"):
                self._meta["analysisStatus"] = data.get("analysisStatus")
            self._update_local_index()
            self._emit_desk("upsert", {
                "reconstructionStatus": self._meta.get("reconstructionStatus"),
                "analysisStatus": self._meta.get("analysisStatus"),
            })

    def note_trump(self, *, trump, declarer, open_flag: bool = False, hand_id=None) -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            payload = {
                "type": "trump",
                "trump": trump,
                "declarer": declarer,
                "trumpOpen": bool(open_flag),
            }
            if hand_id is not None:
                payload["handId"] = hand_id
            self._write(payload)

    def note_decision(self, rec: dict) -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            # Подтверждённый сервером ход учитываем один раз в статистике.
            server_confirmed = bool(rec.get("server_confirmed") or rec.get("serverConfirmed"))
            confirmed = bool(rec.get("confirmed")) and server_confirmed
            if confirmed or rec.get("count_decision"):
                self.decisions += 1
            # lifecycle: chosen | sent | server_confirmed | rejected | unconfirmed
            status = rec.get("confirm_status") or rec.get("confirmStatus")
            if not status:
                if rec.get("rejected"):
                    status = "rejected"
                elif server_confirmed:
                    status = "server_confirmed"
                elif rec.get("sent"):
                    status = "sent"
                elif rec.get("chosen_action") is not None or rec.get("intent_action") is not None:
                    status = "chosen"
                else:
                    status = "unconfirmed"
            payload = {
                "type": "decision",
                "decisionId": rec.get("decision_id"),
                "handId": rec.get("hand_id"),
                "phase": rec.get("phase"),
                "seat": rec.get("seat"),
                "source": rec.get("source"),
                "agent": rec.get("agent") or self.agent,
                "ruleId": (rec.get("outcome") or {}).get("rule_id") if isinstance(rec.get("outcome"), dict) else rec.get("rule_id"),
                "reason": rec.get("reason"),
                "legalActions": rec.get("legal_actions"),
                "intentAction": rec.get("intent_action"),
                "chosenAction": rec.get("chosen_action"),
                "confirmedAction": rec.get("confirmed_action") if server_confirmed else None,
                "confirmed": confirmed,
                "serverConfirmed": server_confirmed,
                "confirmStatus": status,
                "rejected": bool(rec.get("rejected")),
                "sendAttempts": rec.get("send_attempts"),
                "state": rec.get("state"),
                "matchScoresBefore": rec.get("match_scores_before"),
                "incomplete": bool(rec.get("incomplete")),
                "observedOnly": bool(rec.get("observed_only")),
            }
            # Не тащим секреты и сырые cookies — их в journal нет.
            self._write(payload)
            self._update_local_index()

    def note_play(self, *, seat: int, card: int, card_token: str, us: bool, hand_id=None, trick_no=None) -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            self._write({
                "type": "play",
                "handId": hand_id,
                "trickNo": trick_no,
                "seat": int(seat),
                "card": int(card),
                "cardToken": card_token,
                "us": bool(us),
                "observed": True,
            })

    def note_trick_end(self, *, hand_id, trick_no, cards, winner, winner_name=None, points=None) -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            self._write({
                "type": "trick_end",
                "handId": hand_id,
                "trickNo": trick_no,
                "cards": cards,
                "winner": winner,
                "winnerName": winner_name,
                "points": points,
                "label": "Взятка выиграна" if winner is not None and self.seat is not None and int(winner) == int(self.seat) else (
                    "Взятка проиграна" if winner is not None else "Требует разбора"
                ),
            })

    def note_hand_end(self, *, hand_id, scores=None, points=None, result: str = "игра") -> None:
        with self._lock:
            if not self.game_id or self.status != "running":
                return
            if scores is not None:
                self.scores = list(scores)
            self._write({
                "type": "hand_end",
                "handId": hand_id,
                "scores": list(scores or []),
                "points": list(points or []) if points is not None else None,
                "result": result,
            })
            self._update_local_index()
            self._emit_desk("upsert")

    def note_error(self, message: str, **fields) -> None:
        with self._lock:
            if not self.game_id:
                return
            payload = {"type": "error", "message": str(message)}
            payload.update(fields)
            self._write(payload)

    def finalize(self, status: str, *, result: str | None = None, scores=None, reason: str = "") -> None:
        with self._lock:
            self._finalize_locked(status, result=result, scores=scores, reason=reason)

    def _finalize_locked(self, status: str, *, result: str | None = None, scores=None, reason: str = "") -> None:
        if not self.game_id or self.status in ("completed", "packed", "processing"):
            return
        if status not in ("completed", "interrupted", "incomplete", "error"):
            status = "interrupted"
        self.status = status
        if result is not None:
            self.result = result
        if scores is not None:
            self.scores = list(scores)
        ended = time.time()
        self._meta["endedAt"] = ended
        self._write({
            "type": "final",
            "status": status,
            "result": self.result,
            "scores": self.scores,
            "hands": self.hands,
            "decisions": self.decisions,
            "endedAt": ended,
            "reason": reason or None,
        })
        self._update_local_index()
        self._emit_desk("upsert", {"endedAt": ended})
