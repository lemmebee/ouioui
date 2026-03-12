"""Larousse dictionary scraper. French definitions + English→French translation fallback."""

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import os

import deepl
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote


_deepl_client = None
_session = requests.Session()
_cache: dict[str, dict] = {}
MAX_CACHE = 200


def _get_deepl():
    global _deepl_client
    if _deepl_client is None:
        key = os.environ.get("DEEPL_API_KEY", "")
        if key:
            _deepl_client = deepl.Translator(key)
    return _deepl_client

FR_URL = "https://www.larousse.fr/dictionnaires/francais/"
EN_FR_URL = "https://www.larousse.fr/dictionnaires/anglais-francais/"


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text.strip())


def _fetch_soup(url: str) -> BeautifulSoup:
    resp = _session.get(url, timeout=10)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def _parse_fr_soup(soup: BeautifulSoup, word: str) -> dict:
    """Extract definitions + metadata from a single French Larousse page."""
    resolved = word
    definitions = []
    synonyms = []
    homonyms = []
    word_types = []

    ad = soup.select_one(".AdresseDefinition")
    if ad:
        text = "".join(ad.find_all(string=True, recursive=False)).strip()
        text = re.sub(r"[*!?\u00a0]", "", text).strip()
        if text:
            resolved = text.split(",")[0].strip()

    # Extract definitions (replaces larousse_api call)
    for ul in soup.find_all("ul"):
        if ul.get("class") and "Definitions" in ul.get("class"):
            for li in ul.find_all("li"):
                d = _normalize(re.sub(r"<.*?>", "", str(li)))
                if d:
                    definitions.append(d)
            break

    for el in soup.select(".CatgramDefinition"):
        t = _normalize(el.get_text()).replace("Conjugaison", "").strip()
        if t and t not in word_types:
            word_types.append(t)

    for el in soup.select(".Synonymes a"):
        t = _normalize(el.get_text())
        if t and t not in synonyms:
            synonyms.append(t)

    for el in soup.select("#homonyme a"):
        t = _normalize(el.get_text())
        if t and t not in homonyms:
            homonyms.append(t)

    return {
        "word": resolved, "definitions": definitions,
        "word_types": word_types, "synonyms": synonyms, "homonyms": homonyms,
    }


def _lookup_en_fr(word: str) -> dict:
    """Translate an English word to French via Larousse bilingual dictionary."""
    soup = _fetch_soup(EN_FR_URL + quote(word.lower()))

    translations = []
    for li in soup.select("li.itemZONESEM"):
        indicator = li.select_one(".Indicateur")
        trads = []
        for t in li.select(".Traduction"):
            text = _normalize(t.get_text())
            text = re.sub(r"^Conjugaison", "", text).strip().rstrip(",")
            # Separate trailing gender markers glued to the word
            text = re.sub(r"([a-z\u00e0-\u00ff])([mf]+)$", r"\1 (\2.)", text)
            if text:
                trads.append(text)

        ind = ""
        if indicator:
            ind = _normalize(indicator.get_text()) + " "

        if trads:
            translations.append(f"{ind}{', '.join(trads)}".strip())

    return {
        "word": word,
        "definitions": translations,
        "synonyms": [],
        "homonyms": [],
        "source": "en-fr",
    }


def translate_defs(definitions: list[str], target: str) -> list[str]:
    """Translate a list of French texts to target language via DeepL."""
    if not definitions:
        return []
    translator = _get_deepl()
    if not translator:
        return []
    try:
        results = translator.translate_text(definitions, source_lang="FR", target_lang=target)
        return [r.text for r in results]
    except Exception:
        return []


def full_lookup(word: str) -> dict:
    """Full lookup with translations. Pipelines all HTTP calls through one pool."""
    word = word.strip()
    key = word.lower()
    if key in _cache:
        return _cache[key]

    with ThreadPoolExecutor(max_workers=4) as pool:
        # Batch 1: FR page + EN-FR page in parallel
        f_soup = pool.submit(lambda: _fetch_soup(FR_URL + quote(key)))
        f_enfr = pool.submit(lambda: _lookup_en_fr(word) if " " not in word else None)

        # Wait for FR soup, then immediately submit DeepL (overlaps with EN-FR if still running)
        meta = {"word": word, "definitions": [], "word_types": [], "synonyms": [], "homonyms": []}
        try:
            soup = f_soup.result()
            meta = _parse_fr_soup(soup, word)
        except Exception:
            pass

        defs = meta["definitions"]
        f_en = pool.submit(translate_defs, defs, "EN-US")
        f_ar = pool.submit(translate_defs, defs, "AR")
        f_word_ar = pool.submit(translate_defs, [meta["word"]], "AR")
        f_word_en = pool.submit(translate_defs, [meta["word"]], "EN-US")

        # Gather remaining
        translations = []
        try:
            en_result = f_enfr.result()
            if en_result:
                translations = en_result.get("definitions", [])
        except Exception:
            pass

        defs_en = f_en.result()
        defs_ar = f_ar.result()
        word_ar_list = f_word_ar.result()
        word_ar = word_ar_list[0] if word_ar_list else ""
        word_en_list = f_word_en.result()
        word_en = word_en_list[0] if word_en_list else ""

    source = "fr"
    if not defs and translations:
        source = "en-fr"

    result = {
        "word": meta["word"],
        "word_ar": word_ar,
        "word_en": word_en,
        "definitions": defs,
        "definitions_en": defs_en,
        "definitions_ar": defs_ar,
        "word_types": meta["word_types"],
        "synonyms": meta["synonyms"],
        "homonyms": meta["homonyms"],
        "translations": translations,
        "source": source,
    }

    if len(_cache) >= MAX_CACHE:
        _cache.clear()
    _cache[key] = result
    return result


def lookup(word: str) -> dict:
    """Legacy wrapper — use full_lookup for results with translations."""
    r = full_lookup(word)
    return {k: v for k, v in r.items() if k not in ("definitions_en", "definitions_ar")}
