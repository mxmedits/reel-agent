"""Tiny SQLite state: which recordings were processed, every clip and where it was posted."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL, status TEXT, processed_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY, source TEXT, game TEXT, series_no INTEGER,
  start REAL, "end" REAL, peak REAL, score REAL, quality INTEGER,
  status TEXT,              -- ready | queued | posted | skipped | failed | rejected
  file TEXT, copy_json TEXT, render_json TEXT, created_at TEXT,
  ig_status TEXT, ig_id TEXT, ig_url TEXT,
  yt_status TEXT, yt_id TEXT,
  posted_at TEXT, attempts INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS counters (game TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS insights (
  clip_id TEXT PRIMARY KEY, fetched_at TEXT, views INTEGER, reach INTEGER, saved INTEGER,
  shares INTEGER, comments INTEGER, likes INTEGER, total_interactions INTEGER, avg_watch_ms INTEGER
);
CREATE TABLE IF NOT EXISTS replied (
  comment_id TEXT PRIMARY KEY, clip_id TEXT, bucket TEXT, reply TEXT, hidden INTEGER, at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, last_run TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # sources -------------------------------------------------------------
    def source_seen(self, path: Path) -> bool:
        r = self.conn.execute("SELECT 1 FROM sources WHERE path=?", (str(path),)).fetchone()
        return r is not None

    def mark_source(self, path: Path, status: str, note: str = ""):
        from .safety import scrub
        note = scrub(note)
        st = path.stat() if path.exists() else None
        self.conn.execute(
            "INSERT OR REPLACE INTO sources(path,size,mtime,status,processed_at,note) VALUES(?,?,?,?,?,?)",
            (str(path), st.st_size if st else 0, st.st_mtime if st else 0, status, now_iso(), note))
        self.conn.commit()

    # counters ------------------------------------------------------------
    def next_series(self, game: str) -> int:
        r = self.conn.execute("SELECT n FROM counters WHERE game=?", (game,)).fetchone()
        n = (r["n"] if r else 0) + 1
        self.conn.execute("INSERT OR REPLACE INTO counters(game,n) VALUES(?,?)", (game, n))
        self.conn.commit()
        return n

    def peek_series(self, game: str) -> int:
        r = self.conn.execute("SELECT n FROM counters WHERE game=?", (game,)).fetchone()
        return (r["n"] if r else 0) + 1

    # clips ---------------------------------------------------------------
    def add_clip(self, **f):
        f.setdefault("created_at", now_iso())
        for k in ("copy_json", "render_json"):
            if isinstance(f.get(k), (dict, list)):
                f[k] = json.dumps(f[k], ensure_ascii=False)
        cols = ",".join(f'"{k}"' for k in f)
        self.conn.execute(f"INSERT OR REPLACE INTO clips({cols}) VALUES({','.join('?' * len(f))})",
                          list(f.values()))
        self.conn.commit()

    def update(self, clip_id: str, **f):
        if isinstance(f.get("error"), str):      # error texts can quote a URL with a token in it
            from .safety import scrub
            f["error"] = scrub(f["error"])
        for k in ("copy_json", "render_json"):
            if isinstance(f.get(k), (dict, list)):
                f[k] = json.dumps(f[k], ensure_ascii=False)
        sets = ",".join(f'"{k}"=?' for k in f)
        self.conn.execute(f"UPDATE clips SET {sets} WHERE id=?", [*f.values(), clip_id])
        self.conn.commit()

    def get(self, clip_id: str):
        return self.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()

    def clips(self, *statuses: str):
        if statuses:
            q = f"SELECT * FROM clips WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at"
            return self.conn.execute(q, statuses).fetchall()
        return self.conn.execute("SELECT * FROM clips ORDER BY created_at").fetchall()

    def posted_since(self, iso_utc: str) -> list:
        return self.conn.execute(
            "SELECT * FROM clips WHERE posted_at IS NOT NULL AND posted_at >= ? ORDER BY posted_at",
            (iso_utc,)).fetchall()

    def last_posted(self):
        return self.conn.execute(
            "SELECT * FROM clips WHERE posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT 1").fetchone()


    # engagement / analytics ---------------------------------------------
    def save_insights(self, clip_id: str, m: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO insights VALUES(?,?,?,?,?,?,?,?,?,?)",
            (clip_id, now_iso(), m.get("views"), m.get("reach"), m.get("saved"), m.get("shares"),
             m.get("comments"), m.get("likes"), m.get("total_interactions"), m.get("ig_reels_avg_watch_time")))
        self.conn.commit()

    def performance(self) -> list:
        return self.conn.execute(
            "SELECT c.*, i.views, i.reach, i.saved, i.shares, i.comments, i.likes, i.total_interactions, "
            "i.avg_watch_ms FROM clips c JOIN insights i ON i.clip_id = c.id ORDER BY c.posted_at").fetchall()

    def was_replied(self, comment_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM replied WHERE comment_id=?", (comment_id,)).fetchone() is not None

    def mark_replied(self, comment_id: str, clip_id: str, bucket: str, reply: str | None, hidden: bool):
        self.conn.execute("INSERT OR REPLACE INTO replied VALUES(?,?,?,?,?,?)",
                          (comment_id, clip_id, bucket, reply, int(hidden), now_iso()))
        self.conn.commit()

    def replies_since(self, iso_utc: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM replied WHERE reply IS NOT NULL AND at >= ?",
                                 (iso_utc,)).fetchone()[0]

    def job_last_run(self, name: str) -> str | None:
        r = self.conn.execute("SELECT last_run FROM jobs WHERE name=?", (name,)).fetchone()
        return r["last_run"] if r else None

    def job_done(self, name: str):
        self.conn.execute("INSERT OR REPLACE INTO jobs VALUES(?,?)", (name, now_iso()))
        self.conn.commit()


def copy_of(row) -> dict:
    return json.loads(row["copy_json"] or "{}")


def render_of(row) -> dict:
    return json.loads(row["render_json"] or "{}")
