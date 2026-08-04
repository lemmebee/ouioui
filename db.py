import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "ouioui.db"))

CURATED_WORDS = [
    "bonjour", "merci", "maison", "chat", "livre", "soleil", "fleur",
    "eau", "pain", "fromage", "amour", "jardin", "chanson", "etoile", "voyage",
]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added to `words` after the original schema. Listed once so the CREATE
# TABLE below and the ALTER-based migration cannot drift apart.
WORD_COLUMNS = [
    ("synonyms", "TEXT DEFAULT '[]'"),
    ("homonyms", "TEXT DEFAULT '[]'"),
    ("word_ar", "TEXT DEFAULT ''"),
    ("word_en", "TEXT DEFAULT ''"),
    ("word_types", "TEXT DEFAULT '[]'"),
    ("definitions_en", "TEXT DEFAULT '[]'"),
    ("definitions_ar", "TEXT DEFAULT '[]'"),
    ("translations", "TEXT DEFAULT '[]'"),
    # Wiktionary-sourced fields
    ("senses", "TEXT DEFAULT '[]'"),
    ("ipa", "TEXT DEFAULT '[]'"),
    ("etymology", "TEXT DEFAULT ''"),
    ("derived", "TEXT DEFAULT '[]'"),
    ("proverbs", "TEXT DEFAULT '[]'"),
    ("related", "TEXT DEFAULT '[]'"),
    ("hypernyms", "TEXT DEFAULT '[]'"),
    ("forms", "TEXT DEFAULT '[]'"),
    ("glosses_en_native", "TEXT DEFAULT '[]'"),
    ("conjugation", "TEXT DEFAULT '[]'"),
    # Which dictionary.RECORD_VERSION produced this row; drives the backfill.
    ("version", "INTEGER DEFAULT 0"),
]

# JSON-encoded list columns, decoded on read.
JSON_LIST_FIELDS = [
    "word_types", "definitions", "definitions_en", "definitions_ar", "translations",
    "synonyms", "homonyms", "senses", "ipa", "derived", "proverbs", "related",
    "hypernyms", "forms", "glosses_en_native", "conjugation",
]


def init_db():
    conn = get_db()
    # Migrations: add any column missing from an existing table.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(words)").fetchall()]
    if cols:
        for name, decl in WORD_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE words ADD COLUMN {name} {decl}")
        conn.commit()

    added = ",\n            ".join(f"{name:<18} {decl}" for name, decl in WORD_COLUMNS)
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            word        TEXT NOT NULL UNIQUE,
            definitions TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            {added}
        );
        CREATE TABLE IF NOT EXISTS dict_cache (
            word        TEXT PRIMARY KEY,
            payload     TEXT NOT NULL,
            fetched_at  TEXT DEFAULT (datetime('now'))
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


def _dedupe_conjugation(table: list[dict]) -> tuple[list[dict], bool]:
    """Drop repeated forms within each tense. Returns (table, changed)."""
    changed = False
    for group in table:
        seen, forms = set(), []
        for f in group.get("forms", []):
            if f not in seen:
                seen.add(f)
                forms.append(f)
        if len(forms) != len(group.get("forms", [])):
            group["forms"] = forms
            changed = True
    return table, changed


def repair_conjugations():
    """Fix rows stored before homonymous verb entries were deduplicated.

    A page with two etymologies of the same verb ("écarter") contributed two
    full tables, so every tense was listed twice. Repaired in place rather than
    by re-fetching, because a re-fetch would rebuild the senses too and, with no
    DeepL quota left, would replace existing translations with empty ones.
    """
    conn = get_db()
    for table, key, col in (("words", "id", "conjugation"), ("dict_cache", "word", "payload")):
        rows = conn.execute(f"SELECT {key}, {col} FROM {table}").fetchall()
        for r in rows:
            raw = r[col]
            if not raw or raw == "[]":
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if col == "payload":
                conj, changed = _dedupe_conjugation(data.get("conjugation") or [])
                if not changed:
                    continue
                data["conjugation"] = conj
                new = json.dumps(data, ensure_ascii=False)
            else:
                conj, changed = _dedupe_conjugation(data)
                if not changed:
                    continue
                new = json.dumps(conj, ensure_ascii=False)
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE {key} = ?", (new, r[key]))
    conn.commit()
    conn.close()


## --- Lookup cache ---
# A word costs two kaikki fetches plus two DeepL calls the first time it is
# looked up, and nothing on every lookup after that.

def cache_get(word: str) -> dict | None:
    conn = get_db()
    r = conn.execute("SELECT payload FROM dict_cache WHERE word = ?", (word.lower(),)).fetchone()
    conn.close()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except json.JSONDecodeError:
        return None


def cache_put(word: str, record: dict):
    conn = get_db()
    conn.execute(
        "INSERT INTO dict_cache (word, payload, fetched_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(word) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
        (word.lower(), json.dumps(record, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def _is_stale(record: dict) -> bool:
    """Whether a cached record must be re-fetched rather than served.

    Two cases: it was built by an older record version and so is missing fields
    added since, or it was built before DEEPL_API_KEY existed and so carries no
    translations. Neither would ever self-correct, because a cache hit does not
    re-fetch.
    """
    import dictionary
    if record.get("version") != dictionary.RECORD_VERSION:
        return True
    if not dictionary.deepl_available():
        return False
    return any(not s.get("en") for s in record.get("senses", []))


def cached_lookup(word: str) -> dict:
    """Lookup a word, hitting the network only on a cache miss."""
    import dictionary
    word = word.strip()
    cached = cache_get(word)
    if cached is not None and not _is_stale(cached):
        return cached
    record = dictionary.full_lookup(word)
    if record.get("senses"):
        cache_put(word, record)
        # The resolved headword may differ from what was typed ("Maison" -> "maison").
        if record["word"].lower() != word.lower():
            cache_put(record["word"], record)
    return record


## --- Words ---

WORD_FIELDS = [
    "word", "word_ar", "word_en", "word_types", "definitions", "definitions_en",
    "definitions_ar", "translations", "synonyms", "homonyms", "senses", "ipa",
    "etymology", "derived", "proverbs", "related", "hypernyms", "forms",
    "glosses_en_native", "conjugation", "version",
]


def _encode(record: dict, field: str):
    value = record.get(field)
    if field in JSON_LIST_FIELDS:
        return json.dumps(value or [], ensure_ascii=False)
    if field == "version":
        return int(value or 0)
    return value or ""


def _row_to_word(r: sqlite3.Row) -> dict:
    keys = r.keys()
    out = {"id": r["id"]}
    for field in WORD_FIELDS:
        if field not in keys:
            continue
        raw = r[field]
        out[field] = json.loads(raw or "[]") if field in JSON_LIST_FIELDS else (raw or "")
    for extra in ("created_at", "next_review", "repetitions", "easiness", "interval"):
        if extra in keys:
            out[extra] = r[extra]
    return out


def save_word(record: dict) -> int | None:
    """Persist a lookup record. Returns None if the word is already saved."""
    columns = ", ".join(WORD_FIELDS)
    placeholders = ", ".join("?" for _ in WORD_FIELDS)
    conn = get_db()
    try:
        cur = conn.execute(
            f"INSERT INTO words ({columns}) VALUES ({placeholders})",
            tuple(_encode(record, f) for f in WORD_FIELDS),
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
    return [_row_to_word(r) for r in rows]


def backfill_word_fields():
    """Re-fetch saved words produced by an older record version, so they pick up
    fields added since (senses, IPA, conjugation, ...). Runs once per word per
    version bump; review progress is never touched."""
    conn = get_db()
    all_rows = conn.execute("SELECT id, word, senses, version FROM words").fetchall()
    conn.close()
    # Same staleness rule as the lookup cache, so a row whose translation failed
    # at save time is retried instead of being stuck until the next version bump.
    rows = [
        r for r in all_rows
        if _is_stale({"version": r["version"], "senses": json.loads(r["senses"] or "[]")})
    ]
    for r in rows:
        try:
            record = cached_lookup(r["word"])
        except Exception:
            continue
        if not record.get("senses"):
            continue
        # Only write fields the new record actually fills, so legacy-only data
        # (notably homonyms, which Wiktionary does not carry) survives.
        fields = [
            f for f in WORD_FIELDS
            if f != "word" and _encode(record, f) not in ("", "[]")
        ]
        assignments = ", ".join(f"{f}=?" for f in fields)
        conn = get_db()
        conn.execute(
            f"UPDATE words SET {assignments} WHERE id=?",
            tuple(_encode(record, f) for f in fields) + (r["id"],),
        )
        conn.commit()
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
    return _row_to_word(r) if r else None


def update_word_senses(word: str, senses: list[dict]):
    """Persist sense changes (e.g. an example translated on demand) if the word is saved."""
    conn = get_db()
    conn.execute(
        "UPDATE words SET senses = ? WHERE word = ?",
        (json.dumps(senses, ensure_ascii=False), word),
    )
    conn.commit()
    conn.close()


def delete_word(word_id: int):
    conn = get_db()
    conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
    conn.commit()
    conn.close()


def get_due_card() -> dict | None:
    conn = get_db()
    r = conn.execute("""
        SELECT w.*, r.repetitions, r.easiness, r.interval, r.next_review
        FROM words w JOIN reviews r ON w.id = r.word_id
        WHERE r.next_review <= date('now')
        ORDER BY r.next_review ASC LIMIT 1
    """).fetchone()
    conn.close()
    return _row_to_word(r) if r else None


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
    return _row_to_word(r) if r else None
