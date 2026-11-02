"""Durable conversations and idempotent turn intents, separate from 3D jobs."""
from contextlib import contextmanager
import json
import sqlite3
import uuid

from .store import now, valid_id, valid_key

CHAT_TERMINAL = {"COMPLETED", "FAILED"}


class ChatStore:
    def __init__(self, directory):
        self.path = directory / "chat.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, created TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, conversation TEXT NOT NULL, "
                       "request_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_turns ON turns(conversation, created)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, tid):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM turns WHERE id=?", (valid_id(tid),)).fetchone()
        if not row:
            raise KeyError("消息不存在")
        return json.loads(row[0])

    def by_request(self, key):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM turns WHERE request_key=?", (valid_key(key),)).fetchone()
        if not row:
            raise KeyError("尚未找到这条消息")
        return json.loads(row[0])

    def conversation(self, cid):
        valid_id(cid)
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                raise KeyError("对话不存在")
            turns = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM turns WHERE conversation=? ORDER BY created, id", (cid,))]
        return {"id": cid, "turns": turns}

    def pending(self):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM turns WHERE json_extract(payload,'$.status') NOT IN ('COMPLETED','FAILED')")]

    def create(self, message, cid, key):
        valid_key(key)
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
            raise ValueError("请输入 1–2000 字消息")
        message = message.strip()
        if cid:
            valid_id(cid)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload FROM turns WHERE request_key=?", (key,)).fetchone()
            if previous:
                turn = json.loads(previous[0])
                if (turn["message"], turn["requestConversationId"]) != (message, cid):
                    raise ValueError("同一消息标识不能用于不同内容或对话")
                return turn, False
            if cid and not db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone():
                raise KeyError("对话不存在")
            active = db.execute("SELECT conversation FROM turns WHERE "
                                "json_extract(payload,'$.status') NOT IN ('COMPLETED','FAILED')").fetchall()
            if len(active) >= 16 or any(r[0] == cid for r in active):
                raise ValueError("上一条消息仍在处理中，请稍候再发")
            stamp, conversation_id = now(), cid or uuid.uuid4().hex
            if not cid:
                db.execute("INSERT INTO conversations VALUES (?,?)", (conversation_id, stamp))
            turn = dict(id=uuid.uuid4().hex, conversationId=conversation_id, requestConversationId=cid,
                        requestKey=key, message=message, status="QUEUED", assistant="", jobId=None,
                        createdAt=stamp, updatedAt=stamp, agent="chat_supervisor")
            db.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (turn["id"], conversation_id, key,
                                                               json.dumps(turn, ensure_ascii=False), stamp))
            return turn, True

    def update(self, tid, **fields):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM turns WHERE id=?", (valid_id(tid),)).fetchone()
            if not row:
                raise KeyError("消息不存在")
            turn = json.loads(row[0])
            if turn["status"] in CHAT_TERMINAL:
                return turn
            turn.update(fields, updatedAt=now())
            db.execute("UPDATE turns SET payload=? WHERE id=?", (json.dumps(turn, ensure_ascii=False), tid))
            return turn
