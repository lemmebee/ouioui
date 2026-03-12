# OuiOui

French vocabulary builder with spaced repetition.

Search French words via Larousse, save them, and review with SM-2 flashcards.

## Features

- **Search** — Look up French words (Larousse scraper) with English & Arabic translations (DeepL)
- **Vocabulary** — Save words with definitions, synonyms, homonyms, English & Arabic translations
- **Expressions** — Save French expressions/phrases with English meanings for study
- **Quiz** — Spaced repetition flashcards (SM-2 algorithm) for both words and expressions
- **Word of the Day** — Random word from your collection on the home page

## Setup

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

App runs at `http://localhost:8000`.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DEEPL_API_KEY` | No | Enables English/Arabic translations. App works without it. |

## Tech stack

FastAPI, HTMX, Tailwind CSS, SQLite, BeautifulSoup (Larousse scraping), DeepL API.

## Deploy

Configured for Render (see `render.yaml`) with Docker.

```bash
docker build -t ouioui .
docker run -p 8080:8080 ouioui
```
