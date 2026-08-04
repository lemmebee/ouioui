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
# Without a DeepL key there is nothing behind the Traduire buttons, so hide them.
templates.env.globals["deepl_enabled"] = dictionary.deepl_available()

# Word of the day cache: {date_str: word_dict}
_wotd_cache: dict[str, dict | None] = {}


def _toast(message: str, kind: str = "success") -> dict[str, str]:
    """Header telling the page to pop a toast. Used where the swapped-in HTML
    is empty, so a deletion would otherwise give no feedback at all."""
    return {"HX-Trigger": json.dumps({"toast": {"message": message, "kind": kind}})}


@app.on_event("startup")
def startup():
    db.init_db()
    db.repair_conjugations()
    threading.Thread(target=db.backfill_word_fields, daemon=True).start()


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    today = date.today().isoformat()
    if today not in _wotd_cache:
        w = db.random_word()
        if not w:
            # Fallback: pick a curated word and look it up
            curated = random.choice(db.CURATED_WORDS)
            try:
                result = db.cached_lookup(curated)
                w = result if result["senses"] else None
            except Exception:
                w = None
        _wotd_cache.clear()
        _wotd_cache[today] = w

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "wotd": _wotd_cache.get(today),
        "word_count": db.word_count(),
        "expression_count": db.expression_count(),
        "due_count": db.due_count() + db.due_expression_count(),
    })


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request):
    return templates.TemplateResponse(request, "search.html", {"request": request})


@app.get("/vocab", response_class=HTMLResponse)
def vocab_page(request: Request):
    return templates.TemplateResponse(request, "vocab.html", {
        "request": request,
        "words": db.get_all_words(),
    })


@app.get("/expressions", response_class=HTMLResponse)
def expressions_page(request: Request):
    return templates.TemplateResponse(request, "expressions.html", {
        "request": request,
        "expressions": db.get_all_expressions(),
    })


@app.get("/word/{word_id}", response_class=HTMLResponse)
def word_detail(request: Request, word_id: int):
    word = db.get_word(word_id)
    if not word:
        return templates.TemplateResponse(
            request, "word.html", {"request": request, "word": None}, status_code=404
        )
    return templates.TemplateResponse(request, "word.html", {"request": request, "w": word})


@app.get("/quiz", response_class=HTMLResponse)
def quiz_page(request: Request):
    return templates.TemplateResponse(request, "quiz.html", {"request": request})


# --- HTMX API ---

@app.post("/api/lookup", response_class=HTMLResponse)
def lookup(request: Request, word: str = Form("")):
    word = word.strip()
    if not word:
        return templates.TemplateResponse(request, "partials/results.html", {
            "request": request, "error": "Entrer un mot",
        })

    try:
        result = db.cached_lookup(word)
    except Exception:
        return templates.TemplateResponse(request, "partials/results.html", {
            "request": request, "error": f"Impossible de trouver \"{word}\"",
        })

    if not result["senses"]:
        return templates.TemplateResponse(request, "partials/results.html", {
            "request": request, "error": f"Aucune définition trouvée pour \"{word}\"",
        })

    return templates.TemplateResponse(request, "partials/results.html", {
        "request": request,
        "r": result,
        "already_saved": db.word_exists(result["word"]),
    })


@app.post("/api/save", response_class=HTMLResponse)
def save_word(word: str = Form(...)):
    """The full record already sits in the lookup cache, so only the word travels."""
    try:
        record = db.cached_lookup(word)
    except Exception:
        return HTMLResponse('<span class="text-red-500 text-sm">Échec de la sauvegarde</span>')
    if not record.get("senses"):
        return HTMLResponse('<span class="text-red-500 text-sm">Rien à sauvegarder</span>')
    if db.save_word(record):
        return HTMLResponse('<span class="text-green-600 text-sm font-medium">&#10003; Sauvegardé</span>')
    return HTMLResponse('<span class="text-stone-400 text-sm">Déjà sauvegardé</span>')


@app.post("/api/translate-example", response_class=HTMLResponse)
def translate_example(
    request: Request,
    word: str = Form(...),
    sense_index: int = Form(...),
    example_index: int = Form(...),
):
    """Translate one extra example on demand, then store it so it stays translated."""
    try:
        record = db.cached_lookup(word)
        example = dictionary.translate_example(record, sense_index, example_index)
    except Exception:
        return HTMLResponse('<span class="text-red-500 text-xs">Traduction indisponible</span>')

    if not example["en"] and not example["ar"]:
        # DeepL unavailable or refused; say so rather than swapping in an empty box.
        return HTMLResponse('<span class="text-stone-400 text-xs">Traduction indisponible</span>')

    db.cache_put(word, record)
    if record["word"].lower() != word.lower():
        db.cache_put(record["word"], record)
    db.update_word_senses(record["word"], record["senses"])

    return templates.TemplateResponse(request, "partials/example_translation.html", {
        "request": request, "example": example,
    })


@app.delete("/api/words/{word_id}", response_class=HTMLResponse)
def delete_word(word_id: int):
    db.delete_word(word_id)
    return HTMLResponse("", headers=_toast("Mot supprimé"))


@app.post("/api/expressions", response_class=HTMLResponse)
def add_expression(request: Request, french: str = Form(...), english: str = Form(...), note: str = Form("")):
    french, english, note = french.strip(), english.strip(), note.strip()
    if not french or not english:
        return HTMLResponse('<span class="text-red-500 text-sm">Français et anglais requis</span>')
    result = db.save_expression(french, english, note)
    if result:
        return templates.TemplateResponse(request, "partials/expression_list.html", {
            "request": request,
            "expressions": db.get_all_expressions(),
        })
    return HTMLResponse('<span class="text-stone-400 text-sm">Expression déjà sauvegardée</span>')


@app.delete("/api/expressions/{expr_id}", response_class=HTMLResponse)
def delete_expression(expr_id: int):
    db.delete_expression(expr_id)
    return HTMLResponse("", headers=_toast("Expression supprimée"))


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
def quiz_next(request: Request):
    card = _next_due_card()
    if card is None:
        return templates.TemplateResponse(request, "partials/quiz_card.html", {
            "request": request,
            "done": _has_any_items(),
            "empty": not _has_any_items(),
        })
    remaining = _total_due()
    return templates.TemplateResponse(request, "partials/quiz_card.html", {
        "request": request,
        "card": card,
        "remaining": remaining,
    })


@app.post("/api/quiz/rate", response_class=HTMLResponse)
def quiz_rate(request: Request, word_id: int = Form(None), expr_id: int = Form(None), quality: int = Form(...)):
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
        return templates.TemplateResponse(request, "partials/quiz_card.html", {
            "request": request,
            "done": _has_any_items(),
            "empty": not _has_any_items(),
        })
    remaining = _total_due()
    return templates.TemplateResponse(request, "partials/quiz_card.html", {
        "request": request,
        "card": next_card,
        "remaining": remaining,
    })
