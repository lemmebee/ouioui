"""Run: pip install httpx && python test_magic.py"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["MAGIC_WORD"] = "baguette"

from fastapi.testclient import TestClient  # noqa: E402

import app as ouioui  # noqa: E402
import magic  # noqa: E402

c = TestClient(ouioui.app, base_url="https://test")
c.__enter__()  # runs startup so the DB tables exist

r = c.get("/vocab", follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == "/magic?next=/vocab", r.headers
assert c.get("/vocab", headers={"hx-request": "true"}).status_code == 401
assert c.get("/static/style.css").status_code == 200

r = c.post("/magic", data={"word": "nope", "next": "/vocab"}, follow_redirects=False)
assert r.status_code == 401 and magic.COOKIE not in c.cookies

r = c.post("/magic", data={"word": "baguette", "next": "https://evil.example"}, follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == "/", r.headers
assert c.get("/vocab", follow_redirects=False).status_code == 200

magic._fails.clear()
c2 = TestClient(ouioui.app, base_url="https://test")
for _ in range(magic.MAX_FAILS):
    c2.post("/magic", data={"word": "x"}, headers={"cf-connecting-ip": "1.2.3.4"})
r = c2.post("/magic", data={"word": "baguette"}, headers={"cf-connecting-ip": "1.2.3.4"})
assert r.status_code == 429, "locked out even with the right word"
print("ok")
