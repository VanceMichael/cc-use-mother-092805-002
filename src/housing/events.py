"""只增事件存储。

后端不更新任何历史记录，所有状态变化都追加为一条事件。事件按序号
递增并与上一条事件的哈希串联，任何删除、重排或事后篡改都会在
``verify`` 时暴露。事件可落盘为 JSONL，进程重启后原样回放，因此
到期整改、待验收事项等不会丢失。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class EventStoreError(Exception):
    """事件流损坏或被篡改。"""


@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    type: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def to_line(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "at": self.at,
                "type": self.type,
                "actor": self.actor,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
                "hash": self.hash,
            },
            ensure_ascii=False,
        )


def _digest(seq: int, at: str, type_: str, actor: str, payload: dict[str, Any], prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "at": at, "type": type_, "actor": actor, "payload": payload, "prev_hash": prev_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


GENESIS = "0" * 64


class EventStore:
    """内存事件流，可选绑定一个 JSONL 文件持久化。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[Event] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(self._from_dict(json.loads(line)))
            self.verify()

    @staticmethod
    def _from_dict(raw: dict[str, Any]) -> Event:
        return Event(
            seq=raw["seq"],
            at=raw["at"],
            type=raw["type"],
            actor=raw["actor"],
            payload=raw["payload"],
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
        )

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(self, at: str, type_: str, actor: str, payload: dict[str, Any]) -> Event:
        seq = len(self._events) + 1
        prev_hash = self._events[-1].hash if self._events else GENESIS
        digest = _digest(seq, at, type_, actor, payload, prev_hash)
        event = Event(seq, at, type_, actor, payload, prev_hash, digest)
        self._events.append(event)
        if self._path:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_line() + "\n")
        return event

    def replay(self) -> Iterable[Event]:
        return tuple(self._events)

    def verify(self) -> None:
        """逐条复核序号、哈希链，发现历史被改动立即报错。"""
        prev = GENESIS
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                raise EventStoreError(f"事件序号断裂：应为{index}，实为{event.seq}")
            if event.prev_hash != prev:
                raise EventStoreError(f"事件{index}哈希链断裂")
            if _digest(event.seq, event.at, event.type, event.actor, event.payload, event.prev_hash) != event.hash:
                raise EventStoreError(f"事件{index}内容哈希不匹配")
            prev = event.hash
