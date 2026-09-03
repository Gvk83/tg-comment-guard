"""Минимальное хранилище: журнал срабатываний и хеши для поиска повторов.

Полные тексты сообщений по умолчанию не сохраняются — только хеш,
по которому нельзя восстановить исходный текст.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from difflib import SequenceMatcher
from pathlib import Path

DUPLICATE_WINDOW_SEC = 24 * 3600
# Насколько похожим должен быть текст, чтобы считаться тем же образцом спама.
SAMPLE_SIMILARITY = 0.82
MIN_SAMPLE_LEN = 20
CLEANUP_EVERY_SEC = 3600


class Storage:
    def __init__(self, path: Path, store_text: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.store_text = store_text
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._last_cleanup = 0.0

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER,
                score      INTEGER NOT NULL,
                reason     TEXT NOT NULL,
                action     TEXT NOT NULL,
                deleted    INTEGER NOT NULL DEFAULT 0,
                banned     INTEGER NOT NULL DEFAULT 0,
                sample     TEXT
            );
            CREATE TABLE IF NOT EXISTS seen_messages (
                hash    TEXT NOT NULL,
                user_id INTEGER,
                ts      INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_seen_hash ON seen_messages(hash);
            CREATE TABLE IF NOT EXISTS spam_samples (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                skeleton TEXT NOT NULL,
                preview  TEXT NOT NULL,
                ts       INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS known_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ts      INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        self.db.commit()

    # ------------------------------------------------------------- повторы

    @staticmethod
    def _hash(normalized: str) -> str:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def check_duplicate(self, normalized: str, user_id: int | None) -> bool:
        """True, если такое же сообщение недавно прислал ДРУГОЙ пользователь."""
        if len(normalized) < 25:
            return False
        h = self._hash(normalized)
        now = int(time.time())
        row = self.db.execute(
            "SELECT 1 FROM seen_messages WHERE hash=? AND ts>? AND user_id IS NOT ? LIMIT 1",
            (h, now - DUPLICATE_WINDOW_SEC, user_id),
        ).fetchone()
        self.db.execute(
            "INSERT INTO seen_messages(hash, user_id, ts) VALUES (?,?,?)", (h, user_id, now)
        )
        self.db.commit()
        self._maybe_cleanup(now)
        return row is not None

    def _maybe_cleanup(self, now: int) -> None:
        if now - self._last_cleanup < CLEANUP_EVERY_SEC:
            return
        self._last_cleanup = now
        self.db.execute("DELETE FROM seen_messages WHERE ts < ?", (now - DUPLICATE_WINDOW_SEC,))
        self.db.commit()

    # ------------------------------------------------- новые участники чата

    def is_new_member(self, chat_id: int, user_id: int | None) -> bool:
        """True для первого сообщения пользователя в этом чате."""
        if user_id is None:
            return False
        row = self.db.execute(
            "SELECT 1 FROM known_users WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        if row:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO known_users(chat_id, user_id, ts) VALUES (?,?,?)",
            (chat_id, user_id, int(time.time())),
        )
        self.db.commit()
        return True

    # ------------------------------------------------- образцы спама

    @staticmethod
    def skeleton(normalized: str) -> str:
        """Текст без цифр, пунктуации и пробелов — чтобы «5000 в день»
        и «7000 в день» считались одним и тем же объявлением."""
        return re.sub(r"[^а-яa-z]+", "", normalized)

    def add_sample(self, normalized: str, preview: str) -> bool:
        """Запоминает образец спама. False, если такой уже есть или текст короткий."""
        skel = self.skeleton(normalized)
        if len(skel) < MIN_SAMPLE_LEN:
            return False
        if self.match_sample(normalized):
            return False
        self.db.execute(
            "INSERT INTO spam_samples(skeleton, preview, ts) VALUES (?,?,?)",
            (skel, preview[:200], int(time.time())),
        )
        self.db.commit()
        return True

    def match_sample(self, normalized: str) -> bool:
        """True, если текст совпадает с уже известным образцом спама."""
        skel = self.skeleton(normalized)
        if len(skel) < MIN_SAMPLE_LEN:
            return False
        for (known,) in self.db.execute("SELECT skeleton FROM spam_samples"):
            # Длины сильно расходятся — сравнивать нет смысла.
            if not 0.6 <= len(skel) / len(known) <= 1.7:
                continue
            if SequenceMatcher(None, skel, known).ratio() >= SAMPLE_SIMILARITY:
                return True
        return False

    def list_samples(self, limit: int = 20) -> list[tuple]:
        return self.db.execute(
            "SELECT id, preview, ts FROM spam_samples ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def delete_sample(self, sample_id: int) -> bool:
        cur = self.db.execute("DELETE FROM spam_samples WHERE id=?", (sample_id,))
        self.db.commit()
        return cur.rowcount > 0

    def count_samples(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM spam_samples").fetchone()[0])

    # -------------------------------------------------------------- журнал

    def log_incident(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        score: int,
        reason: str,
        action: str,
        deleted: bool,
        banned: bool,
        text: str = "",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO incidents(ts, chat_id, user_id, score, reason, action, deleted, banned, sample)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                chat_id,
                user_id,
                score,
                reason,
                action,
                int(deleted),
                int(banned),
                text[:400] if self.store_text else None,
            ),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def incident_text(self, incident_id: int) -> str | None:
        row = self.db.execute(
            "SELECT sample FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        return row[0] if row else None

    def stats(self, days: int = 7) -> tuple[int, int, int]:
        since = int(time.time()) - days * 86400
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(deleted),0), COALESCE(SUM(banned),0)"
            " FROM incidents WHERE ts > ?",
            (since,),
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def cleanup_old(self, days: int) -> int:
        """Удаляет записи журнала старше указанного срока."""
        if days <= 0:
            return 0
        cur = self.db.execute(
            "DELETE FROM incidents WHERE ts < ?", (int(time.time()) - days * 86400,)
        )
        self.db.execute(
            "DELETE FROM known_users WHERE ts < ?", (int(time.time()) - days * 86400,)
        )
        self.db.commit()
        return cur.rowcount

    def recent(self, limit: int = 10) -> list[tuple]:
        """Последние срабатывания: время, кто, баллы, причина, удалено, забанен."""
        return self.db.execute(
            "SELECT ts, user_id, score, reason, deleted, banned, sample FROM incidents"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def purge_samples(self) -> int:
        """Удаляет сохранённые тексты сообщений из журнала."""
        cur = self.db.execute("UPDATE incidents SET sample=NULL WHERE sample IS NOT NULL")
        self.db.commit()
        return cur.rowcount

    def close(self) -> None:
        self.db.close()
