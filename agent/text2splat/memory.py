"""Local, source-attributed preferences and task episodes; no model or embeddings."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3

from .store import now, valid_id

STOP = {"一个", "生成", "模型", "测试", "物件", "希望", "想要", "请帮", "创建", "任务", "这个",
        "记住", "我的", "偏好", "喜欢", "以后",
        "the", "a", "an", "create", "generate", "model", "please", "test", "remember", "that", "i", "prefer", "like"}


def terms(text):
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words - STOP


def clipped(value, size):
    return value[:size] if isinstance(value, str) else ""


class MemoryStore:
    def __init__(self, directory):
        self.path = Path(directory) / "memory.sqlite3"

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                       "payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS forgotten (id TEXT PRIMARY KEY)")
            db.execute("CREATE INDEX IF NOT EXISTS memory_kind_date ON memories(kind, updated_at DESC)")
            with db:
                yield db
        finally:
            db.close()

    def list(self, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("记忆分页参数无效")
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM memories ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", (limit, offset))]

    def set_preference(self, key, value, *, source_job_id=None, evidence=None, source_turn_id=None):
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", key):
            raise ValueError("偏好名称只能包含小写字母、数字、下划线和短横线")
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= 200:
            raise ValueError("偏好内容应为 2–200 字")
        if source_job_id is not None:
            valid_id(source_job_id)
        if source_turn_id is not None:
            valid_id(source_turn_id)
        stamp, mid = now(), "pref:" + key
        item = dict(id=mid, kind="preference", value=value.strip(), source="user_explicit",
                    source_job_id=source_job_id, source_turn_id=source_turn_id, evidence=evidence, updated_at=stamp)
        with self.connection() as db:
            # An explicit new user update is allowed to restore a deleted preference.
            db.execute("DELETE FROM forgotten WHERE id=?", (mid,))
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "payload=excluded.payload, updated_at=excluded.updated_at",
                       (mid, "preference", json.dumps(item, ensure_ascii=False), stamp, stamp))
        return item

    def capture_explicit_preference(self, prompt, job_id=None, *, turn_id=None):
        # Only a standalone explicit remember request. Object adjectives are not a user profile.
        patterns = [r"(?:^|\n)\s*(?:请)?记住(?:我的偏好[：:]\s*|我(?:喜欢|偏好)\s*)([^\n。！？!?]{2,200})(?=[\n。！？!?]|$)",
                    r"(?:^|\n)\s*remember (?:that )?I (?:prefer|like) ([^\n.!?]{2,200})(?=[\n.!?]|$)"]
        for pattern in patterns:
            match = re.search(pattern, prompt, re.IGNORECASE)
            if match and len(match[1].strip()) >= 2:
                return self.set_preference("explicit-description", match[1], source_job_id=job_id,
                                           source_turn_id=turn_id, evidence=match[0].strip())
        return None

    def record_job(self, job):
        status = job.get("status")
        if status not in {"READY", "CONCEPT_READY", "NEEDS_REVIEW", "FAILED"}:
            return False
        # Exclude startup/configuration failures and model-only stops without generation evidence.
        if not job.get("referenceSetId") and not job.get("agentFailure"):
            return False
        jid = valid_id(job["id"])
        result = job.get("result") or {}
        verified = status == "READY" and any(
            isinstance(f, dict) and f.get("name") == "gaussians.ply" and f.get("url")
            and isinstance(f.get("bytes"), (int, float)) and f["bytes"] > 0
            for f in result.get("files", []))
        if status == "READY" and not verified:
            return False
        outcome = "verified_3d" if verified else "concept_only" if status == "CONCEPT_READY" else status.lower()
        review = job.get("review") or {}
        item = dict(id="episode:" + jid, kind="episode", source="task_record", source_job_id=jid,
                    description=clipped(job.get("prompt"), 2000), mode=job.get("mode"), outcome=outcome,
                    title=clipped((job.get("spec") or {}).get("title"), 80),
                    review=clipped(review.get("reason"), 300),
                    failure=clipped((job.get("agentFailure") or {}).get("message"), 400),
                    artifact_verified=verified, updated_at=job.get("updatedAt") or now())
        with self.connection() as db:
            cursor = db.execute("INSERT OR IGNORE INTO memories SELECT ?,?,?,?,? "
                                "WHERE NOT EXISTS (SELECT 1 FROM forgotten WHERE id=?)",
                                (item["id"], "episode", json.dumps(item, ensure_ascii=False),
                                 item["updated_at"], item["updated_at"], item["id"]))
        return cursor.rowcount == 1

    def recall(self, prompt, exclude_job_id=None):
        with self.connection() as db:
            preferences = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM memories WHERE kind='preference' ORDER BY updated_at DESC, id LIMIT 4")]
            episodes = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM memories WHERE kind='episode' ORDER BY updated_at DESC, id LIMIT 300")]
        query, ranked = terms(prompt), []
        for episode in episodes:
            if episode["source_job_id"] == exclude_job_id:
                continue
            words = terms(episode["description"] + " " + episode["title"])
            overlap = query & words
            if overlap:
                score = len(overlap) / math.sqrt(max(1, len(query) * len(words)))
                ranked.append((score, episode["updated_at"], episode))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # Do not send artifact URLs, foreign task state or complete histories to the model.
        recalled = [{k: p[k] for k in ("id", "kind", "value", "source", "updated_at")} for p in preferences]
        for _, _, e in ranked[:3]:
            recalled.append({**{k: e[k] for k in ("id", "kind", "source_job_id", "outcome", "artifact_verified", "updated_at")},
                             "description": e["description"][:240], "title": e["title"],
                             "review": e["review"][:160], "failure": e["failure"][:200]})
        return recalled

    def forget(self, mid=None):
        if mid is not None and not re.fullmatch(r"(?:pref:[a-z0-9_-]{1,64}|episode:[a-f0-9]{32})", mid):
            raise ValueError("记忆 ID 无效")
        with self.connection() as db:
            if mid is None:
                db.execute("INSERT OR IGNORE INTO forgotten SELECT id FROM memories")
                count = db.execute("DELETE FROM memories").rowcount
            else:
                db.execute("INSERT OR IGNORE INTO forgotten VALUES (?)", (mid,))
                count = db.execute("DELETE FROM memories WHERE id=?", (mid,)).rowcount
        return count
