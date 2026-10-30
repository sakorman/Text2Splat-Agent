import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review"}


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("无效资源 ID")
    return value


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.assets = root / "assets"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.database = root / "mcp.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, kind TEXT,
                    payload TEXT, state TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS links (key TEXT PRIMARY KEY, value TEXT);
            """)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, key, kind, payload):
        if not isinstance(key, str) or not 1 <= len(key) <= 160:
            raise ValueError("无效请求标识")
        encoded = json.dumps(payload, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            if row:
                if row["kind"] != kind or row["payload"] != encoded:
                    raise ValueError("同一请求标识不能用于不同内容")
                return json.loads(row["state"])
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(state,'$.status') IN ('queued','running')").fetchone()[0]
            if active >= 32:
                raise ValueError("生成队列已满，请稍后再试")
            jid = uuid.uuid4().hex
            state = dict(id=jid, status="queued", stage="queued", message="等待生成", assets=[])
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)", (jid, key, kind, encoded, json.dumps(state), time.time()))
            return state

    def get(self, jid):
        with self.connect() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (valid_id(jid),)).fetchone()
        if row is None:
            raise ValueError("任务不存在")
        return json.loads(row[0])

    def by_request(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT state FROM jobs WHERE request_key=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, jid, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (valid_id(jid),)).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            state = json.loads(row[0])
            if state["status"] in TERMINAL:
                return state
            state.update(fields)
            db.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state, ensure_ascii=False), jid))
            return state

    def recover(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE json_extract(state,'$.status') IN ('queued','running')")]
        for jid in ids:
            self.update(jid, status="failed", stage="interrupted", message="服务重启，生成已中断。请创建新版本；不会自动重发模型请求。")

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE json_extract(state,'$.status')='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            state = json.loads(row["state"])
            state.update(status="running")
            db.execute("UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), row["id"]))
            return row["id"], row["kind"], json.loads(row["payload"])

    def link(self, key, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO links VALUES(?,?)", (key, json.dumps(value)))
                return value
            row = db.execute("SELECT value FROM links WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def directory(self, asset_id):
        return self.assets / valid_id(asset_id)

    def manifest(self, asset_id):
        try:
            return json.loads((self.directory(asset_id) / "manifest.json").read_text())
        except FileNotFoundError:
            raise ValueError("图片集合不存在或尚未完成") from None

    def save_manifest(self, asset_id, data):
        directory = self.directory(asset_id)
        directory.mkdir(exist_ok=True)
        temp = directory / "manifest.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temp.replace(directory / "manifest.json")

    def save_image(self, asset_id, index, raw):
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 16 MB")
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > 24_000_000 or min(source.size) < 512:
                raise ValueError("生成图片尺寸不符合要求")
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2048, 2048))
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            directory = self.directory(asset_id)
            directory.mkdir(exist_ok=True)
            name = f"{index:02d}.png"
            image.save(directory / name, "PNG")
        return dict(name=name, sha256=digest, url=f"/api/graphics/assets/{asset_id}/{name}")
