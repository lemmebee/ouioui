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
    # Migrations
    cols = [row[1] for row in conn.execute("PRAGMA table_info(words)").fetchall()]
    if cols and "word_ar" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN word_ar TEXT DEFAULT ''")
        conn.commit()
    if cols and "word_en" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN word_en TEXT DEFAULT ''")
        conn.commit()
    if cols and "word_types" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN word_types TEXT DEFAULT '[]'")
        conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word        TEXT NOT NULL UNIQUE,
            word_ar     TEXT DEFAULT '',
            word_en     TEXT DEFAULT '',
            word_types  TEXT DEFAULT '[]',
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
        CREATE TABLE IF NOT EXISTS expressions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            french      TEXT NOT NULL UNIQUE,
            english     TEXT NOT NULL,
            note        TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS expression_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            expr_id     INTEGER NOT NULL UNIQUE REFERENCES expressions(id) ON DELETE CASCADE,
            repetitions INTEGER DEFAULT 0,
            easiness    REAL DEFAULT 2.5,
            interval    INTEGER DEFAULT 0,
            next_review TEXT DEFAULT (date('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.close()


def save_word(word: str, definitions: list[str], synonyms: list[str], homonyms: list[str], word_ar: str = "", word_en: str = "", word_types: list[str] | None = None) -> int | None:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO words (word, word_ar, word_en, word_types, definitions, synonyms, homonyms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (word, word_ar, word_en, json.dumps(word_types or []), json.dumps(definitions), json.dumps(synonyms), json.dumps(homonyms)),
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
            "word_ar": r["word_ar"] or "",
            "word_en": r["word_en"] or "",
            "word_types": json.loads(r["word_types"] or "[]"),
            "definitions": json.loads(r["definitions"]),
            "synonyms": json.loads(r["synonyms"]),
            "homonyms": json.loads(r["homonyms"]),
            "created_at": r["created_at"],
            "next_review": r["next_review"],
        })
    return result


def backfill_word_fields():
    """Backfill word_en and word_types for words missing them."""
    import dictionary
    conn = get_db()
    rows = conn.execute(
        "SELECT id, word FROM words WHERE (word_en = '' OR word_en IS NULL) OR (word_types = '[]' OR word_types IS NULL)"
    ).fetchall()
    for r in rows:
        try:
            result = dictionary.full_lookup(r["word"])
            word_en = result.get("word_en", "")
            word_types = json.dumps(result.get("word_types", []))
            conn.execute(
                "UPDATE words SET word_en = ?, word_types = ? WHERE id = ?",
                (word_en, word_types, r["id"]),
            )
            conn.commit()
        except Exception:
            continue
    conn.close()


def word_exists(word: str) -> bool:
    conn = get_db()
    r = conn.execute("SELECT 1 FROM words WHERE word = ?", (word,)).fetchone()
    conn.close()
    return r is not None


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


## --- Expressions ---

def save_expression(french: str, english: str, note: str = "") -> int | None:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO expressions (french, english, note) VALUES (?, ?, ?)",
            (french, english, note),
        )
        expr_id = cur.lastrowid
        conn.execute("INSERT INTO expression_reviews (expr_id) VALUES (?)", (expr_id,))
        conn.commit()
        return expr_id
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_all_expressions() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT e.*, r.next_review FROM expressions e LEFT JOIN expression_reviews r ON e.id = r.expr_id ORDER BY e.created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "french": r["french"],
            "english": r["english"],
            "note": r["note"] or "",
            "created_at": r["created_at"],
            "next_review": r["next_review"],
        }
        for r in rows
    ]


def delete_expression(expr_id: int):
    conn = get_db()
    conn.execute("DELETE FROM expressions WHERE id = ?", (expr_id,))
    conn.commit()
    conn.close()


def expression_count() -> int:
    conn = get_db()
    r = conn.execute("SELECT COUNT(*) as c FROM expressions").fetchone()
    conn.close()
    return r["c"]


def get_due_expression() -> dict | None:
    conn = get_db()
    r = conn.execute("""
        SELECT e.id, e.french, e.english, e.note,
               r.repetitions, r.easiness, r.interval, r.next_review
        FROM expressions e JOIN expression_reviews r ON e.id = r.expr_id
        WHERE r.next_review <= date('now')
        ORDER BY r.next_review ASC LIMIT 1
    """).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "french": r["french"],
        "english": r["english"],
        "note": r["note"] or "",
        "repetitions": r["repetitions"],
        "easiness": r["easiness"],
        "interval": r["interval"],
        "next_review": r["next_review"],
        "type": "expression",
    }


def due_expression_count() -> int:
    conn = get_db()
    r = conn.execute(
        "SELECT COUNT(*) as c FROM expression_reviews WHERE next_review <= date('now')"
    ).fetchone()
    conn.close()
    return r["c"]


def update_expression_review(expr_id: int, repetitions: int, easiness: float, interval: int, next_review: str):
    conn = get_db()
    conn.execute(
        "UPDATE expression_reviews SET repetitions=?, easiness=?, interval=?, next_review=?, updated_at=datetime('now') WHERE expr_id=?",
        (repetitions, easiness, interval, next_review, expr_id),
    )
    conn.commit()
    conn.close()


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
