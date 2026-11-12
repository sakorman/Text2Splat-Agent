"""Atomic job journal, compatible with the previous Java workflow records."""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import uuid

TERMINAL = {"READY", "CONCEPT_READY", "NEEDS_REVIEW", "FAILED", "CANCELLED"}
_owners = set()
_owners_lock = threading.Lock()


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效任务 ID")
    return value


def valid_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,160}", value):
        raise ValueError("请求标识格式错误")
    return value


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobStore:
    def __init__(self, root):
        self.directory = (Path(root) / "workflows").resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.lock = None
        with _owners_lock:
            if self.directory in _owners:
                raise RuntimeError("已有 Agent 使用这个数据目录")
            lock = (self.directory / ".lock").open("a+")
            try:
                # POSIX record lock also conflicts with the old Java FileChannel lock.
                fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeError("已有 Agent 使用这个数据目录") from None
            self.lock = lock
            _owners.add(self.directory)

    def close(self):
        with _owners_lock:
            if self.lock:
                self.lock.close()
                self.lock = None
                _owners.discard(self.directory)

    def get(self, jid):
        path = self.directory / (valid_id(jid) + ".json")
        with self.mutex:
            if not path.exists() or path.is_symlink():
                raise KeyError("任务不存在")
            try:
                job = json.loads(path.read_text())
                if not isinstance(job, dict) or job.get("id") != jid:
                    raise ValueError()
                return job
            except (OSError, ValueError):
                raise RuntimeError("无法读取本地任务记录") from None

    def list(self):
        with self.mutex:
            jobs = [self.get(p.stem) for p in self.directory.glob("*.json")
                    if re.fullmatch(r"[a-f0-9]{32}", p.stem)]
            return sorted(jobs, key=lambda j: j.get("createdAt", ""), reverse=True)

    def by_request(self, key):
        valid_key(key)
        for job in self.list():
            if job.get("requestKey") == key:
                return job
        raise KeyError("尚未找到这个提交对应的任务")

    def create(self, prompt, mode, parent_id, key, *, prompt_source="delegated"):
        valid_key(key)
        if not isinstance(prompt, str) or not 2 <= len(prompt.strip()) <= 2000 or mode not in {"CONCEPT", "THREE_D"}:
            raise ValueError("请提供 2–2000 字描述和有效的生成模式")
        prompt, parent_id = prompt.strip(), parent_id or ""
        with self.mutex:
            jobs = self.list()
            for job in jobs:
                if job.get("requestKey") == key:
                    if (job["prompt"], job["mode"], job.get("parentJobId", "")) != (prompt, mode, parent_id):
                        raise ValueError("同一请求标识不能用于不同内容")
                    return job, False
            if parent_id:
                self.get(parent_id)
            if sum(j["status"] not in TERMINAL for j in jobs) >= 16:
                raise ValueError("任务队列已满，请稍后再试")
            stamp = now()
            job = dict(id=uuid.uuid4().hex, prompt=prompt, promptSource=prompt_source, mode=mode, parentJobId=parent_id,
                       requestKey=key, status="QUEUED", stage="queued", message="等待开始",
                       cancelRequested=False, createdAt=stamp, updatedAt=stamp,
                       referenceAssets=[], viewAssets=[])
            self._save(job)
            return deepcopy(job), True

    def update(self, jid, **fields):
        with self.mutex:
            job = self.get(jid)
            if job["status"] in TERMINAL:
                return job
            job.update(fields)
            if job.get("cancelRequested") and job["status"] != "CANCELLED":
                job["status"] = "CANCELLING"
            job["updatedAt"] = now()
            self._save(job)
            return deepcopy(job)

    def _save(self, job):
        path = self.directory / (valid_id(job["id"]) + ".json")
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w") as stream:
            json.dump(job, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
