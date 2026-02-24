import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "ouioui.db"

CURATED_WORDS = [
    "bonjour", "merci", "maison", "chat", "livre", "soleil", "fleur",
    "eau", "pain", "fromage", "amour", "jardin", "chanson", "etoile", "voyage",
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word        TEXT NOT NULL UNIQUE,
            definitions TEXT NOT NULL,
            synonyms    TEXT DEFAULT '[]',
            homonyms    TEXT DEFAULT '[]',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id     INTEGER NOT NULL UNIQUE REFERENCES words(id) ON DELETE CASCADE,
            repetitions INTEGER DEFAULT 0,
            easiness    REAL DEFAULT 2.5,
            interval    INTEGER DEFAULT 0,
            next_review TEXT DEFAULT (date('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.close()


def save_word(word: str, definitions: list[str], synonyms: list[str], homonyms: list[str]) -> int | None:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO words (word, definitions, synonyms, homonyms) VALUES (?, ?, ?, ?)",
            (word, json.dumps(definitions), json.dumps(synonyms), json.dumps(homonyms)),
        )
        word_id = cur.lastrowid
        conn.execute("INSERT INTO reviews (word_id) VALUES (?)", (word_id,))
        conn.commit()
        return word_id
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_all_words() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT w.*, r.next_review FROM words w LEFT JOIN reviews r ON w.id = r.word_id ORDER BY w.created_at DESC"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "word": r["word"],
            "definitions": json.loads(r["definitions"]),
            "synonyms": json.loads(r["synonyms"]),
            "homonyms": json.loads(r["homonyms"]),
            "created_at": r["created_at"],
            "next_review": r["next_review"],
        })
    return result


def get_word(word_id: int) -> dict | None:
    conn = get_db()
    r = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "word": r["word"],
        "definitions": json.loads(r["definitions"]),
        "synonyms": json.loads(r["synonyms"]),
        "homonyms": json.loads(r["homonyms"]),
    }


def delete_word(word_id: int):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
    conn.commit()
    conn.close()


def get_due_card() -> dict | None:
    conn = get_db()
    r = conn.execute("""
        SELECT w.id, w.word, w.definitions, w.synonyms, w.homonyms,
               r.repetitions, r.easiness, r.interval, r.next_review
        FROM words w JOIN reviews r ON w.id = r.word_id
        WHERE r.next_review <= date('now')
        ORDER BY r.next_review ASC LIMIT 1
    """).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "word": r["word"],
        "definitions": json.loads(r["definitions"]),
        "synonyms": json.loads(r["synonyms"]),
        "homonyms": json.loads(r["homonyms"]),
        "repetitions": r["repetitions"],
        "easiness": r["easiness"],
        "interval": r["interval"],
        "next_review": r["next_review"],
    }


def due_count() -> int:
    conn = get_db()
    r = conn.execute(
        "SELECT COUNT(*) as c FROM reviews WHERE next_review <= date('now')"
    ).fetchone()
    conn.close()
    return r["c"]


def update_review(word_id: int, repetitions: int, easiness: float, interval: int, next_review: str):
    conn = get_db()
    conn.execute(
        "UPDATE reviews SET repetitions=?, easiness=?, interval=?, next_review=?, updated_at=datetime('now') WHERE word_id=?",
        (repetitions, easiness, interval, next_review, word_id),
    )
    conn.commit()
    conn.close()


def word_count() -> int:
    conn = get_db()
    r = conn.execute("SELECT COUNT(*) as c FROM words").fetchone()
    conn.close()
    return r["c"]


def random_word() -> dict | None:
    conn = get_db()
    r = conn.execute("SELECT * FROM words ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "word": r["word"],
        "definitions": json.loads(r["definitions"]),
        "synonyms": json.loads(r["synonyms"]),
        "homonyms": json.loads(r["homonyms"]),
    }
