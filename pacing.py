"""Persistent, per-account pacing for unfollow attempts."""

import random
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    daily_limit: int = 400
    hourly_limit: int = 30
    min_interval_seconds: int = 30
    batch_size: int = 20
    batch_cooldown_seconds: int = 3600
    interval_jitter_seconds: int = 15

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            minimum = 0 if field.name == "interval_jitter_seconds" else 1
            if type(value) is not int or not minimum <= value <= 2_147_483_647:
                raise ValueError(
                    f"UNFOLLOW_{field.name.upper()} must be an integer from {minimum} to 2147483647"
                )

    @classmethod
    def from_env(cls, path: str | Path = ".env") -> "Limits":
        """Load settings from a dotenv file, using defaults for absent keys."""
        names = {f"UNFOLLOW_{field.name.upper()}": field.name for field in fields(cls)}
        try:
            contents = Path(path).read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return cls()

        settings = {}
        for line in contents.splitlines():
            key, separator, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if key not in names:
                continue
            name = names[key]
            if name in settings:
                raise ValueError(f"{path} defines {key} more than once")
            value = value.partition("#")[0].strip()
            if len(value) >= 2 and value[0] in "\"'" and value[0] == value[-1]:
                value = value[1:-1]
            if not separator or not value.isdecimal():
                raise ValueError(f"{path}: {key} must be a nonnegative integer")
            settings[name] = int(value)
        return cls(**settings)


class Pacer:
    def __init__(self, handle: str, limits: Limits, db_path: str | Path = "db.sqlite3") -> None:
        handle = handle.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
            raise ValueError("Enter an X handle using 1–15 letters, digits, or underscores")

        self.handle = handle
        self.limits = limits
        self.db_path = db_path
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS unfollow_attempts (
                    handle TEXT NOT NULL,
                    attempted_at REAL NOT NULL,
                    jitter_seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (handle, attempted_at)
                ) WITHOUT ROWID
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(unfollow_attempts)")}
            if "jitter_seconds" not in columns:
                db.execute(
                    "ALTER TABLE unfollow_attempts ADD jitter_seconds INTEGER NOT NULL DEFAULT 0"
                )

    def next_at(self, now: float | None = None) -> float:
        """Return the next eligible time in Unix seconds, or now if eligible."""
        with closing(sqlite3.connect(self.db_path)) as db:
            return self._next_at(db, time.time() if now is None else now)

    def reserve(self, now: float | None = None) -> float | None:
        """Record an attempt before acting, or return the time to try again."""
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time() if now is None else now
            next_at = self._next_at(db, now)
            if next_at > now:
                return next_at
            jitter = random.randint(0, self.limits.interval_jitter_seconds)
            db.execute(
                "INSERT INTO unfollow_attempts (handle, attempted_at, jitter_seconds) "
                "VALUES (?, ?, ?)",
                (self.handle, now, jitter),
            )
        return None

    def _next_at(self, db: sqlite3.Connection, now: float) -> float:
        limits = self.limits
        rows = db.execute(
            """
            SELECT attempted_at, jitter_seconds FROM unfollow_attempts
            WHERE handle = ? ORDER BY attempted_at DESC LIMIT ?
            """,
            (self.handle, max(limits.daily_limit, limits.hourly_limit, limits.batch_size)),
        ).fetchall()
        if not rows:
            return now

        recent = [row[0] for row in rows]
        latest_at, latest_jitter = rows[0]
        next_at = max(now, latest_at + limits.min_interval_seconds + latest_jitter)
        if len(recent) >= limits.hourly_limit:
            next_at = max(next_at, recent[limits.hourly_limit - 1] + 60 * 60)
        if len(recent) >= limits.daily_limit:
            next_at = max(next_at, recent[limits.daily_limit - 1] + 24 * 60 * 60)

        # A gap at least as long as the cooldown starts a new batch.
        batch = recent[:limits.batch_size]
        cooldown = limits.batch_cooldown_seconds
        if len(batch) == limits.batch_size and all(
            newer - older < cooldown for newer, older in zip(batch, batch[1:])
        ):
            next_at = max(next_at, latest_at + cooldown)

        return next_at
