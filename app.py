import json
import random
import threading
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

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
    threading.Thread(target=db.backfill_word_fields, daemon=True).start()


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
        "expression_count": db.expression_count(),
        "due_count": db.due_count() + db.due_expression_count(),
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


@app.get("/expressions", response_class=HTMLResponse)
async def expressions_page(request: Request):
    return templates.TemplateResponse("expressions.html", {
        "request": request,
        "expressions": db.get_all_expressions(),
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
        result = dictionary.full_lookup(word)
    except Exception:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": f"Impossible de trouver \"{word}\"",
        })

    if not result["definitions"] and not result["translations"]:
        return templates.TemplateResponse("partials/results.html", {
            "request": request, "error": f"Aucune definition trouvee pour \"{word}\"",
        })

    save_data = json.dumps({
        "word": result["word"],
        "word_ar": result.get("word_ar", ""),
        "word_en": result.get("word_en", ""),
        "word_types": json.dumps(result.get("word_types", [])),
        "definitions": json.dumps(result["definitions"]),
        "synonyms": json.dumps(result["synonyms"]),
        "homonyms": json.dumps(result["homonyms"]),
    })

    return templates.TemplateResponse("partials/results.html", {
        "request": request,
        "word": result["word"],
        "word_ar": result.get("word_ar", ""),
        "word_en": result.get("word_en", ""),
        "definitions": result["definitions"],
        "definitions_en": result["definitions_en"],
        "definitions_ar": result["definitions_ar"],
        "translations": result["translations"],
        "word_types": result["word_types"],
        "synonyms": result["synonyms"],
        "homonyms": result["homonyms"],
        "source": result["source"],
        "save_data": save_data,
        "already_saved": db.word_exists(result["word"]),
    })


@app.post("/api/save", response_class=HTMLResponse)
async def save_word(
    word: str = Form(...),
    word_ar: str = Form(""),
    word_en: str = Form(""),
    word_types: str = Form("[]"),
    definitions: str = Form(...),
    synonyms: str = Form("[]"),
    homonyms: str = Form("[]"),
):
    defs = json.loads(definitions)
    syns = json.loads(synonyms)
    homs = json.loads(homonyms)
    wtypes = json.loads(word_types)
    result = db.save_word(word, defs, syns, homs, word_ar=word_ar, word_en=word_en, word_types=wtypes)
    if result:
        return HTMLResponse('<span class="text-green-600 text-sm font-medium">&#10003; Sauvegarde</span>')
    return HTMLResponse('<span class="text-stone-400 text-sm">Deja sauvegarde</span>')


@app.delete("/api/words/{word_id}", response_class=HTMLResponse)
async def delete_word(word_id: int):
    db.delete_word(word_id)
    return HTMLResponse("")


@app.post("/api/expressions", response_class=HTMLResponse)
async def add_expression(request: Request, french: str = Form(...), english: str = Form(...), note: str = Form("")):
    french, english, note = french.strip(), english.strip(), note.strip()
    if not french or not english:
        return HTMLResponse('<span class="text-red-500 text-sm">Francais et anglais requis</span>')
    result = db.save_expression(french, english, note)
    if result:
        return templates.TemplateResponse("partials/expression_list.html", {
            "request": request,
            "expressions": db.get_all_expressions(),
        })
    return HTMLResponse('<span class="text-stone-400 text-sm">Expression deja sauvegardee</span>')


@app.delete("/api/expressions/{expr_id}", response_class=HTMLResponse)
async def delete_expression(expr_id: int):
    db.delete_expression(expr_id)
    return HTMLResponse("")


def _next_due_card() -> dict | None:
    """Return the next due card (word or expression), whichever is older."""
    word_card = db.get_due_card()
    expr_card = db.get_due_expression()
    if word_card and expr_card:
        return word_card if word_card["next_review"] <= expr_card["next_review"] else expr_card
    return word_card or expr_card


def _total_due() -> int:
    return db.due_count() + db.due_expression_count()


def _has_any_items() -> bool:
    return db.word_count() > 0 or db.expression_count() > 0


@app.get("/api/quiz/next", response_class=HTMLResponse)
async def quiz_next(request: Request):
    card = _next_due_card()
    if card is None:
        return templates.TemplateResponse("partials/quiz_card.html", {
            "request": request,
            "done": _has_any_items(),
            "empty": not _has_any_items(),
        })
    remaining = _total_due()
    return templates.TemplateResponse("partials/quiz_card.html", {
        "request": request,
        "card": card,
        "remaining": remaining,
    })


@app.post("/api/quiz/rate", response_class=HTMLResponse)
async def quiz_rate(request: Request, word_id: int = Form(None), expr_id: int = Form(None), quality: int = Form(...)):
    if word_id:
        card = db.get_due_card()
        if card and card["id"] == word_id:
            reps, ease, ivl = sm2(quality, card["repetitions"], card["easiness"], card["interval"])
            next_date = (date.today() + timedelta(days=ivl)).isoformat()
            db.update_review(word_id, reps, ease, ivl, next_date)
    elif expr_id:
        card = db.get_due_expression()
        if card and card["id"] == expr_id:
            reps, ease, ivl = sm2(quality, card["repetitions"], card["easiness"], card["interval"])
            next_date = (date.today() + timedelta(days=ivl)).isoformat()
            db.update_expression_review(expr_id, reps, ease, ivl, next_date)

    # Return next card
    next_card = _next_due_card()
    if next_card is None:
        return templates.TemplateResponse("partials/quiz_card.html", {
            "request": request,
            "done": _has_any_items(),
            "empty": not _has_any_items(),
        })
    remaining = _total_due()
    return templates.TemplateResponse("partials/quiz_card.html", {
        "request": request,
        "card": next_card,
        "remaining": remaining,
    })
