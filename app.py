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
# Passed as a callable, not a value: a key can run out of quota mid-process, and
# the Traduire buttons must disappear when there is nothing behind them.
templates.env.globals["deepl_enabled"] = dictionary.deepl_available

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
        "words": db.get_all_words(struck=False),
        "marked_count": db.word_count(struck=True),
    })


@app.get("/expressions", response_class=HTMLResponse)
def expressions_page(request: Request):
    return templates.TemplateResponse(request, "expressions.html", {
        "request": request,
        "expressions": db.get_all_expressions(struck=False),
        "marked_count": db.expression_count(struck=True),
    })


@app.get("/marked", response_class=HTMLResponse)
def marked_page(request: Request):
    return templates.TemplateResponse(request, "marked.html", {
        "request": request,
        "words": db.get_all_words(struck=True),
        "expressions": db.get_all_expressions(struck=True),
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
def quiz_page(request: Request, mode: str = ""):
    return templates.TemplateResponse(request, "quiz.html", {
        "request": request,
        "mode": mode,
        "marked_total": db.word_count(struck=True) + db.expression_count(struck=True),
    })


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
        # Say which of the two it is: a spent quota comes back next billing period,
        # anything else is worth retrying now.
        reason = ("Quota de traduction atteint" if dictionary.quota_exhausted()
                  else "Traduction indisponible")
        return HTMLResponse(f'<span class="text-stone-400 text-xs">{reason}</span>')

    db.cache_put(word, record)
    if record["word"].lower() != word.lower():
        db.cache_put(record["word"], record)
    db.update_word_senses(record["word"], record["senses"])

    return templates.TemplateResponse(request, "partials/example_translation.html", {
        "request": request, "example": example,
    })


def _on_marked_page(request: Request) -> bool:
    """htmx sends the page the request came from, which tells us whether the
    card just left the main list or the marked one."""
    return (request.headers.get("HX-Current-URL") or "").rstrip("/").endswith("/marked")


def _header_oob(request: Request, kind: str) -> str:
    """Re-rendered list heading for an out-of-band swap, so the totals follow a
    single-card toggle without re-rendering the whole list."""
    marked = _on_marked_page(request)
    count = db.word_count if kind == "words" else db.expression_count
    if marked:
        total, title = count(struck=True), ("Mots marqués" if kind == "words" else "Expressions marquées")
    else:
        total, title = count(struck=False), ("Vocabulaire" if kind == "words" else "Expressions")
    return templates.get_template("partials/list_header.html").render(
        request=request, kind=kind, title=title, total=total,
        marked_count=count(struck=True), on_marked_page=marked, oob=True,
    )


@app.post("/api/words/{word_id}/strike", response_class=HTMLResponse)
def strike_word(request: Request, word_id: int):
    struck = db.toggle_struck("words", word_id)
    msg = "Déplacé vers les marqués" if struck else "Remis dans le vocabulaire"
    return HTMLResponse(_header_oob(request, "words"), headers=_toast(msg, "info"))


@app.post("/api/expressions/{expr_id}/strike", response_class=HTMLResponse)
def strike_expression(request: Request, expr_id: int):
    struck = db.toggle_struck("expressions", expr_id)
    msg = "Déplacé vers les marqués" if struck else "Remis dans les expressions"
    return HTMLResponse(_header_oob(request, "expressions"), headers=_toast(msg, "info"))


@app.post("/api/words/unstrike-all", response_class=HTMLResponse)
def unstrike_all_words(request: Request):
    n = db.unstrike_all("words")
    return HTMLResponse(_header_oob(request, "words"),
                        headers=_toast(f"{n} mot{'s' if n != 1 else ''} démarqué{'s' if n != 1 else ''}"))


@app.post("/api/expressions/unstrike-all", response_class=HTMLResponse)
def unstrike_all_expressions(request: Request):
    n = db.unstrike_all("expressions")
    return HTMLResponse(_header_oob(request, "expressions"),
                        headers=_toast(f"{n} expression{'s' if n != 1 else ''} démarquée{'s' if n != 1 else ''}"))


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


def _next_due_card(marked: bool = False) -> dict | None:
    """Return the next card (word or expression), whichever is older."""
    word_card = db.get_due_card(marked)
    expr_card = db.get_due_expression(marked)
    if word_card and expr_card:
        return word_card if word_card["next_review"] <= expr_card["next_review"] else expr_card
    return word_card or expr_card


def _total_due(marked: bool = False) -> int:
    return db.due_count(marked) + db.due_expression_count(marked)


def _has_any_items(marked: bool = False) -> bool:
    if marked:
        return db.word_count(struck=True) + db.expression_count(struck=True) > 0
    return db.word_count() > 0 or db.expression_count() > 0


@app.post("/api/quiz/reset-marked", response_class=HTMLResponse)
def reset_marked(request: Request):
    """Run the marked deck again without waiting for tomorrow."""
    db.reset_marked_pass()
    return templates.TemplateResponse(request, "partials/quiz_card.html", {
        "request": request,
        "mode": "marked",
        "card": _next_due_card(True),
        "remaining": _total_due(True),
    })


@app.get("/api/quiz/next", response_class=HTMLResponse)
def quiz_next(request: Request, mode: str = ""):
    marked = mode == "marked"
    card = _next_due_card(marked)
    if card is None:
        return templates.TemplateResponse(request, "partials/quiz_card.html", {
            "request": request,
            "mode": mode,
            "done": _has_any_items(marked),
            "empty": not _has_any_items(marked),
        })
    return templates.TemplateResponse(request, "partials/quiz_card.html", {
        "request": request,
        "mode": mode,
        "card": card,
        "remaining": _total_due(marked),
    })


@app.post("/api/quiz/rate", response_class=HTMLResponse)
def quiz_rate(request: Request, word_id: int = Form(None), expr_id: int = Form(None),
              quality: int = Form(...), mode: str = Form("")):
    marked = mode == "marked"
    if word_id:
        card = db.get_due_card(marked)
        if card and card["id"] == word_id:
            reps, ease, ivl = sm2(quality, card["repetitions"], card["easiness"], card["interval"])
            next_date = (date.today() + timedelta(days=ivl)).isoformat()
            db.update_review(word_id, reps, ease, ivl, next_date)
    elif expr_id:
        card = db.get_due_expression(marked)
        if card and card["id"] == expr_id:
            reps, ease, ivl = sm2(quality, card["repetitions"], card["easiness"], card["interval"])
            next_date = (date.today() + timedelta(days=ivl)).isoformat()
            db.update_expression_review(expr_id, reps, ease, ivl, next_date)

    next_card = _next_due_card(marked)
    if next_card is None:
        return templates.TemplateResponse(request, "partials/quiz_card.html", {
            "request": request,
            "mode": mode,
            "done": _has_any_items(marked),
            "empty": not _has_any_items(marked),
        })
    return templates.TemplateResponse(request, "partials/quiz_card.html", {
        "request": request,
        "mode": mode,
        "card": next_card,
        "remaining": _total_due(marked),
    })
