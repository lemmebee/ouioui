import json
import random
from datetime import date, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import dictionary
from sm2 import sm2

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Word of the day cache: {date_str: word_dict}
_wotd_cache: dict[str, dict | None] = {}


@app.on_event("startup")
def startup():
    db.init_db()


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    today = date.today().isoformat()
    if today not in _wotd_cache:
        w = db.random_word()
        if not w:
            # Fallback: pick a curated word and look it up
            curated = random.choice(db.CURATED_WORDS)
            try:
                result = dictionary.lookup(curated)
                w = result if result["definitions"] else None
            except Exception:
                w = None
        _wotd_cache.clear()
        _wotd_cache[today] = w

    return templates.TemplateResponse("index.html", {
        "request": request,
        "wotd": _wotd_cache.get(today),
        "word_count": db.word_count(),
        "due_count": db.due_count(),
    })


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/vocab", response_class=HTMLResponse)
async def vocab_page(request: Request):
    return templates.TemplateResponse("vocab.html", {
        "request": request,
        "words": db.get_all_words(),
    })


@app.get("/quiz", response_class=HTMLResponse)
async def quiz_page(request: Request):
    return templates.TemplateResponse("quiz.html", {"request": request})


# --- HTMX API ---

@app.post("/api/lookup", response_class=HTMLResponse)
async def lookup(request: Request, word: str = Form(...)):
    word = word.strip()
    if not word:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": "Entrer un mot",
        })
    try:
        result = dictionary.lookup(word)
    except Exception:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": f"Impossible de trouver \"{word}\"",
        })

    if not result["definitions"] and not result["translations"]:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": f"Aucune definition trouvee pour \"{word}\"",
        })

    save_data = json.dumps({
        "word": word,
        "definitions": json.dumps(result["definitions"]),
        "synonyms": json.dumps(result["synonyms"]),
        "homonyms": json.dumps(result["homonyms"]),
    })

    return templates.TemplateResponse("partials/results.html", {
        "request": request,
        "word": word,
        "definitions": result["definitions"],
        "translations": result.get("translations", []),
        "synonyms": result["synonyms"],
        "homonyms": result["homonyms"],
        "source": result.get("source", "fr"),
        "save_data": save_data,
    })


@app.post("/api/save", response_class=HTMLResponse)
async def save_word(
    word: str = Form(...),
    definitions: str = Form(...),
    synonyms: str = Form("[]"),
    homonyms: str = Form("[]"),
):
    defs = json.loads(definitions)
    syns = json.loads(synonyms)
    homs = json.loads(homonyms)
    result = db.save_word(word, defs, syns, homs)
    if result:
        return HTMLResponse('<span class="text-green-600 text-sm font-medium">&#10003; Sauvegarde</span>')
    return HTMLResponse('<span class="text-stone-400 text-sm">Deja sauvegarde</span>')


@app.delete("/api/words/{word_id}", response_class=HTMLResponse)
async def delete_word(word_id: int):
    db.delete_word(word_id)
    return HTMLResponse("")


@app.get("/api/quiz/next", response_class=HTMLResponse)
async def quiz_next(request: Request):
    card = db.get_due_card()
    if card is None:
        has_words = db.word_count() > 0
        return templates.TemplateResponse("partials/quiz_card.html", {
            "request": request,
            "done": has_words,
            "empty": not has_words,
        })
    remaining = db.due_count()
    return templates.TemplateResponse("partials/quiz_card.html", {
        "request": request,
        "card": card,
        "remaining": remaining,
    })


@app.post("/api/quiz/rate", response_class=HTMLResponse)
async def quiz_rate(request: Request, word_id: int = Form(...), quality: int = Form(...)):
    card = db.get_due_card()
    if card and card["id"] == word_id:
        reps, ease, ivl = sm2(quality, card["repetitions"], card["easiness"], card["interval"])
        next_date = (date.today() + timedelta(days=ivl)).isoformat()
        db.update_review(word_id, reps, ease, ivl, next_date)

    # Return next card
    next_card = db.get_due_card()
    if next_card is None:
        has_words = db.word_count() > 0
        return templates.TemplateResponse("partials/quiz_card.html", {
            "request": request,
            "done": has_words,
            "empty": not has_words,
        })
    remaining = db.due_count()
    return templates.TemplateResponse("partials/quiz_card.html", {
        "request": request,
        "card": next_card,
        "remaining": remaining,
    })
