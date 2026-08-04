"""French dictionary data from Wiktionary (via kaikki.org structured JSONL) + DeepL.

Three layers:
  1. fr.wiktionary  -> senses, examples, IPA, gender, synonyms, derived, etymology,
                       and word-level EN/AR translations
  2. en.wiktionary  -> native human-written English glosses
  3. DeepL          -> completes every sense to {fr, en, ar} and fills any gap
                       layers 1-2 left open

No HTML scraping: kaikki serves pre-parsed JSON built from Wiktionary dumps.
"""

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import deepl

FR_BASE = "https://kaikki.org/frwiktionary/All%20languages%20combined/meaning/{}.jsonl"
EN_BASE = "https://kaikki.org/dictionary/French/meaning/{}.jsonl"

TIMEOUT = 20
USER_AGENT = "ouioui/1.0 (french study app)"

# Stamped into every record. Bump whenever the record gains or changes a field,
# so cached entries built by an older version are re-fetched instead of served
# with the new field silently missing.
RECORD_VERSION = 2

# fr.wiktionary part-of-speech codes -> display labels
POS_LABELS = {
    "noun": "nom", "verb": "verbe", "adj": "adjectif", "adv": "adverbe",
    "intj": "interjection", "prep": "préposition", "conj": "conjonction",
    "pron": "pronom", "det": "déterminant", "num": "numéral", "article": "article",
    "name": "nom propre", "phrase": "locution", "prefix": "préfixe", "suffix": "suffixe",
    "particle": "particule", "postp": "postposition", "abbrev": "abréviation",
}

GENDER_LABELS = {
    "masculine": "m.", "feminine": "f.", "neuter": "n.",
    "plural": "pl.", "singular": "sing.", "invariable": "inv.",
}

# Verb forms arrive tagged but unordered. Matching on the exact tag set keeps
# tenses that share a tag apart: {indicative, past} is the passé simple, while
# {indicative, past, multiword-construction} is the passé composé.
CONJUGATION_TABLE = [
    ("Indicatif", "Présent", {"indicative", "present"}),
    ("Indicatif", "Imparfait", {"indicative", "imperfect"}),
    ("Indicatif", "Passé simple", {"indicative", "past"}),
    ("Indicatif", "Futur simple", {"indicative", "future"}),
    ("Indicatif", "Passé composé", {"indicative", "past", "multiword-construction"}),
    ("Indicatif", "Plus-que-parfait", {"indicative", "pluperfect"}),
    ("Indicatif", "Passé antérieur", {"indicative", "past", "anterior"}),
    ("Indicatif", "Futur antérieur", {"indicative", "future", "perfect"}),
    ("Subjonctif", "Présent", {"subjunctive", "present"}),
    ("Subjonctif", "Imparfait", {"subjunctive", "imperfect"}),
    ("Subjonctif", "Passé", {"subjunctive", "past"}),
    ("Subjonctif", "Plus-que-parfait", {"subjunctive", "pluperfect"}),
    ("Conditionnel", "Présent", {"conditional", "present"}),
    ("Conditionnel", "Passé", {"conditional", "past"}),
    ("Impératif", "Présent", {"imperative", "present"}),
    ("Impératif", "Passé", {"imperative", "past"}),
    ("Formes impersonnelles", "Infinitif", {"infinitive", "present"}),
    ("Formes impersonnelles", "Infinitif passé", {"infinitive", "past"}),
    ("Formes impersonnelles", "Participe présent", {"participle", "present"}),
    ("Formes impersonnelles", "Participe passé", {"participle", "past"}),
    ("Formes impersonnelles", "Gérondif", {"gerund", "present"}),
    ("Formes impersonnelles", "Gérondif passé", {"gerund", "past"}),
]

_deepl_client = None


def _get_deepl():
    global _deepl_client
    if _deepl_client is None:
        key = os.environ.get("DEEPL_API_KEY", "")
        if key:
            _deepl_client = deepl.Translator(key)
    return _deepl_client


def deepl_available() -> bool:
    """Whether the translation layer is configured. Drives the Traduire buttons."""
    return bool(os.environ.get("DEEPL_API_KEY", ""))


# --- kaikki fetch ---

def _path(word: str) -> str:
    """kaikki shards entries as <first-char>/<first-two-chars>/<word>.jsonl."""
    return f"{quote(word[0])}/{quote(word[:2])}/{quote(word)}"


def _candidates(word: str) -> list[str]:
    """kaikki paths are case-sensitive; try the spellings a user is likely to mean."""
    word = word.strip().replace("'", "’")
    seen, out = set(), []
    for variant in (word, word.lower(), word[:1].upper() + word[1:].lower()):
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    return out


def _fetch_jsonl(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return []
    entries = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _sense_count(entries: list[dict], lang_code: str | None = None) -> int:
    return sum(
        len(e.get("senses", []))
        for e in entries
        if lang_code is None or e.get("lang_code") == lang_code
    )


def _fetch_best_variant(base: str, word: str, score) -> list[dict]:
    """Try spelling variants in parallel, keep the richest page.

    Ranking by content rather than by candidate order matters because kaikki
    paths are case-sensitive: a casually capitalised "Maison" would otherwise
    resolve to the surname entry instead of the 16-sense common noun. Ties fall
    back to the order in _candidates, so an exact-spelling match still wins.
    """
    variants = _candidates(word)
    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        pages = list(pool.map(lambda v: _fetch_jsonl(base.format(_path(v))), variants))

    best, best_score = [], (0,)
    for page in pages:
        current = score(page)
        if any(current) and current > best_score:
            best, best_score = page, current
    return best


def _fetch_fr_page(word: str) -> list[dict]:
    """Every entry on the fr.wiktionary page, across all languages.

    English entries are kept because on fr.wiktionary their glosses are written
    in French, which is what makes an English -> French lookup work.
    """
    return _fetch_best_variant(
        FR_BASE, word,
        lambda page: (_sense_count(page, "fr"), _sense_count(page, "en"), _sense_count(page)),
    )


def _fetch_en_glosses(word: str) -> list[dict]:
    """en.wiktionary's French section: human-written English glosses."""
    return _fetch_best_variant(EN_BASE, word, lambda page: (_sense_count(page),))


# --- merge ---

def _clean_ipa(raw: str) -> str:
    return raw.strip().strip("\\/").strip()


def _pos_label(entry: dict) -> str:
    pos = POS_LABELS.get(entry.get("pos", ""), entry.get("pos", ""))
    gender = " ".join(
        GENDER_LABELS[t] for t in entry.get("tags", []) if t in GENDER_LABELS
    )
    return f"{pos} {gender}".strip()


def _build_conjugation(entries: list[dict]) -> list[dict]:
    """Group a verb's inflected forms into an ordered mood/tense table.

    Returns [] for non-verbs, which is what keeps the caller from rendering a
    conjugation block for nouns.
    """
    buckets: dict[frozenset, list[str]] = {}
    for entry in entries:
        if entry.get("pos") != "verb":
            continue
        for form in entry.get("forms", []):
            value, tags = form.get("form"), form.get("tags", [])
            if not value or not tags or "table-tags" in tags or "inflection-template" in tags:
                continue
            buckets.setdefault(frozenset(tags), []).append(value)

    table = []
    for mood, tense, wanted in CONJUGATION_TABLE:
        forms = buckets.get(frozenset(wanted))
        if forms:
            # A page can carry several homonymous verbs, each with a full table
            # ("écarter" has two etymologies), which would otherwise repeat every
            # tense. Forms include their pronoun, so equal strings are redundant.
            table.append({"mood": mood, "tense": tense, "forms": _dedup(forms)})
    return table


def _dedup(values) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _build_skeleton(word: str, fr_entries: list[dict], en_entries: list[dict]) -> dict:
    """Collapse kaikki entries into one record. No translation work here."""
    ipa, word_types, etymology = [], [], ""
    senses, synonyms, derived, proverbs, related, hypernyms, forms = [], [], [], [], [], [], []
    word_en, word_ar = [], []
    resolved = word
    seen_glosses = set()

    for entry in fr_entries:
        resolved = entry.get("word") or resolved
        for sound in entry.get("sounds", []):
            value = _clean_ipa(sound.get("ipa", ""))
            if value and value not in ipa:
                ipa.append(value)

        label = _pos_label(entry)
        if label and label not in word_types:
            word_types.append(label)
        if not etymology and entry.get("etymology_texts"):
            etymology = entry["etymology_texts"][0]

        # Synonyms are sense-linked upstream; index them so each sense keeps its own.
        by_sense: dict[int, list[str]] = {}
        for syn in entry.get("synonyms", []):
            name = syn.get("word")
            if not name:
                continue
            by_sense.setdefault(syn.get("sense_index", 0), []).append(name)
            if name not in synonyms:
                synonyms.append(name)

        for position, sense in enumerate(entry.get("senses", []), start=1):
            glosses = sense.get("glosses") or []
            if not glosses:
                continue
            gloss = glosses[-1]
            if gloss in seen_glosses:
                continue
            seen_glosses.add(gloss)

            examples = [
                {"fr": ex["text"], "ref": ex.get("ref", ""), "en": "", "ar": ""}
                for ex in sense.get("examples", []) if ex.get("text")
            ]
            senses.append({
                "fr": gloss,
                "en": "",
                "ar": "",
                "pos": label,
                "tags": sense.get("tags", []),
                "topics": sense.get("topics", []),
                "synonyms": _dedup(by_sense.get(position, [])),
                "examples": examples,
            })

        for name, bucket in (("derived", derived), ("proverbs", proverbs),
                             ("related", related), ("hypernyms", hypernyms)):
            for item in entry.get(name, []):
                value = item.get("word")
                if value and value not in bucket:
                    bucket.append(value)

        for form in entry.get("forms", []):
            value = form.get("form")
            if value and value != resolved and not any(f["form"] == value for f in forms):
                forms.append({"form": value, "tags": form.get("tags", [])})

        for tr in entry.get("translations", []):
            bucket = {"en": word_en, "ar": word_ar}.get(tr.get("lang_code"))
            if bucket is None or not tr.get("word"):
                continue
            value = tr["word"]
            if tr.get("roman"):
                value = f"{value} ({tr['roman']})"
            if value not in bucket:
                bucket.append(value)

    native_en = _dedup(
        gloss
        for entry in en_entries
        for sense in entry.get("senses", [])
        for gloss in (sense.get("glosses") or [])
    )

    conjugation = _build_conjugation(fr_entries)
    if conjugation:
        # The conjugation table already lists every inflected form; showing them
        # again as ~97 loose chips would only add noise.
        forms = []

    return {
        "word": resolved,
        "ipa": ipa,
        "word_types": word_types,
        "etymology": etymology,
        "conjugation": conjugation,
        "senses": senses,
        "glosses_en_native": native_en,
        "synonyms": synonyms,
        "derived": derived,
        "proverbs": proverbs,
        "related": related,
        "hypernyms": hypernyms,
        "forms": forms,
        "_word_en_native": word_en,
        "_word_ar_native": word_ar,
    }


# --- DeepL completion layer ---

def translate_batch(texts: list[str], target: str) -> list[str]:
    """Translate French texts to target. Returns [] if DeepL is unavailable."""
    if not texts:
        return []
    translator = _get_deepl()
    if not translator:
        return []
    try:
        results = translator.translate_text(texts, source_lang="FR", target_lang=target)
        return [r.text for r in results]
    except Exception:
        return []


def _complete_with_deepl(record: dict) -> dict:
    """Guarantee every sense carries fr + en + ar, and the headword too.

    Translates each sense gloss plus the first example of each sense. Remaining
    examples stay French-only until translate_example() is called for them.
    """
    senses = record["senses"]
    word_en_native = record.pop("_word_en_native", [])
    word_ar_native = record.pop("_word_ar_native", [])

    # Build one batch so a lookup costs exactly one DeepL call per language.
    batch: list[str] = []
    slots: list[tuple] = []

    for si, sense in enumerate(senses):
        batch.append(sense["fr"])
        slots.append(("sense", si))
        if sense["examples"]:
            batch.append(sense["examples"][0]["fr"])
            slots.append(("example", si, 0))

    need_word_en = not word_en_native
    need_word_ar = not word_ar_native
    if need_word_en or need_word_ar:
        batch.append(record["word"])
        slots.append(("word",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_en = pool.submit(translate_batch, batch, "EN-US")
        f_ar = pool.submit(translate_batch, batch, "AR")
        out_en, out_ar = f_en.result(), f_ar.result()

    def assign(values: list[str], lang: str):
        if len(values) != len(slots):
            return
        for slot, value in zip(slots, values):
            if slot[0] == "sense":
                senses[slot[1]][lang] = value
            elif slot[0] == "example":
                senses[slot[1]]["examples"][slot[2]][lang] = value
            elif slot[0] == "word":
                record[f"_deepl_word_{lang}"] = value

    assign(out_en, "en")
    assign(out_ar, "ar")

    record["word_en"] = word_en_native[0] if word_en_native else record.pop("_deepl_word_en", "")
    record["word_ar"] = word_ar_native[0] if word_ar_native else record.pop("_deepl_word_ar", "")
    record.pop("_deepl_word_en", None)
    record.pop("_deepl_word_ar", None)
    record["word_en_all"] = word_en_native
    record["word_ar_all"] = word_ar_native
    return record


def _add_flat_views(record: dict, source: str) -> dict:
    """Flat definition lists kept alongside senses so existing views keep working."""
    senses = record["senses"]
    record["definitions"] = [s["fr"] for s in senses]
    record["definitions_en"] = [s["en"] for s in senses]
    record["definitions_ar"] = [s["ar"] for s in senses]
    record["homonyms"] = []
    record["source"] = source
    record["version"] = RECORD_VERSION
    return record


# --- public API ---

def full_lookup(word: str) -> dict:
    """Fetch, merge and complete one word. Callers are expected to cache the result."""
    word = word.strip()
    if not word:
        raise ValueError("empty word")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_page = pool.submit(_fetch_fr_page, word)
        f_gloss = pool.submit(_fetch_en_glosses, word)
        page, en_entries = f_page.result(), f_gloss.result()

    fr_entries = [e for e in page if e.get("lang_code") == "fr"]
    en_source = [e for e in page if e.get("lang_code") == "en"]

    # An English headword has no French entry, but its fr.wiktionary glosses are
    # written in French, so they carry the lookup on their own.
    source = "fr"
    if not fr_entries and en_source:
        fr_entries, source = en_source, "en-fr"

    record = _build_skeleton(word, fr_entries, en_entries)

    # French renderings of the English entry, kept separate from the French
    # senses so an English lookup still shows what the word means in French.
    record["translations"] = [] if source == "en-fr" else _dedup(
        gloss
        for entry in en_source
        for sense in entry.get("senses", [])
        for gloss in (sense.get("glosses") or [])
    )

    record = _complete_with_deepl(record)
    return _add_flat_views(record, source)


def translate_example(record: dict, sense_index: int, example_index: int) -> dict:
    """Translate one so-far-untranslated example in place. Returns the example."""
    sense = record["senses"][sense_index]
    example = sense["examples"][example_index]
    if example["en"] and example["ar"]:
        return example

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_en = pool.submit(translate_batch, [example["fr"]], "EN-US")
        f_ar = pool.submit(translate_batch, [example["fr"]], "AR")
        out_en, out_ar = f_en.result(), f_ar.result()

    if out_en:
        example["en"] = out_en[0]
    if out_ar:
        example["ar"] = out_ar[0]
    return example
