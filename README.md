# OuiOui

French vocabulary builder with spaced repetition.

Search French words, save them, and review with SM-2 flashcards.

## Features

- **Search** — Look up French words with definitions, IPA, gender, cited example
  sentences, etymology, synonyms and derived forms
- **Trilingual** — Every sense carries French, English and Arabic
- **Vocabulary** — Save words with their full sense data
- **Expressions** — Save French expressions/phrases with English meanings for study
- **Quiz** — Spaced repetition flashcards (SM-2 algorithm) for both words and expressions
- **Word of the Day** — Random word from your collection on the home page

## Where the data comes from

Three layers, merged per lookup:

| Layer | Provides |
|---|---|
| [kaikki.org](https://kaikki.org) fr-Wiktionary | senses, cited examples, IPA, gender, sense-linked synonyms, derived forms, proverbs, etymology, inflected forms, English/Arabic word translations |
| kaikki.org en-Wiktionary | human-written English glosses |
| DeepL | completes every sense to French + English + Arabic, and fills any translation the first two layers left open |

kaikki serves Wiktionary pre-parsed as JSON, so there is no HTML scraping and no
brittle CSS selectors. The French corpus covers ~2M word forms, comfortably more
than a commercial dictionary.

Typing an English word works too: on fr-Wiktionary, English entries are glossed in
French, so `freedom` resolves to *Liberté*.

Every lookup is cached in the `dict_cache` table, so a word costs two HTTP fetches
plus two DeepL calls the first time and nothing afterwards. Sense glosses and the
first example of each sense are translated up front; remaining examples have a
*Traduire* button that translates on demand and stores the result.

## Setup

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

App runs at `http://localhost:8000`.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DEEPL_API_KEY` | No | Enables the English/Arabic completion layer. Without it, French senses and any translations Wiktionary already carries still work. |
| `DB_PATH` | No | SQLite file location. Defaults to `ouioui.db` next to the app. |

Words saved before this version are re-fetched in the background on startup so
they gain senses, IPA and the rest. Review progress is untouched.

## Tech stack

FastAPI, HTMX, Tailwind CSS, SQLite, DeepL API.

## Deploy

Configured for Render (see `render.yaml`) with Docker.

```bash
docker build -t ouioui .
docker run -p 8080:8080 ouioui
```
