"""Larousse dictionary scraper. French definitions + English→French translation fallback."""

import re
import unicodedata

import requests
from bs4 import BeautifulSoup

from larousse_api import larousse

FR_URL = "https://www.larousse.fr/dictionnaires/francais/"
EN_FR_URL = "https://www.larousse.fr/dictionnaires/anglais-francais/"


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text.strip())


def _fetch_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _lookup_french(word: str) -> dict:
    """Look up a French word."""
    definitions = larousse.get_definitions(word)
    synonyms = []
    homonyms = []

    try:
        soup = _fetch_soup(FR_URL + word.lower())

        for el in soup.select(".Synonymes a"):
            t = _normalize(el.get_text())
            if t and t not in synonyms:
                synonyms.append(t)

        for el in soup.select("#homonyme a"):
            t = _normalize(el.get_text())
            if t and t not in homonyms:
                homonyms.append(t)
    except Exception:
        pass

    return {
        "word": word,
        "definitions": definitions,
        "synonyms": synonyms,
        "homonyms": homonyms,
        "source": "fr",
    }


def _lookup_en_fr(word: str) -> dict:
    """Translate an English word to French via Larousse bilingual dictionary."""
    soup = _fetch_soup(EN_FR_URL + word.lower())

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


def lookup(word: str) -> dict:
    """Look up a word. Tries French + English→French, returns both when available."""
    result = _lookup_french(word)

    # Always try EN→FR translation
    translations = []
    try:
        en_result = _lookup_en_fr(word)
        translations = en_result.get("definitions", [])
    except Exception:
        pass

    result["translations"] = translations

    # If no French definitions but we have translations, mark source as en-fr
    if not result["definitions"] and translations:
        result["source"] = "en-fr"

    return result
