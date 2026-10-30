# ouioui

French flashcards with spaced repetition (SM-2). FastAPI + Jinja templates + HTMX, SQLite. Public at https://ouioui.efk.sh through a Cloudflare tunnel, gated by a magic word.

## Work here
- Python 3.12 (`python:3.12-slim` image). Run: `uvicorn app:app --port 8080` with `DB_PATH`, optional `DEEPL_API_KEY` and `MAGIC_WORD`.
- Check: `python test_magic.py` (needs httpx). Add a `test_*.py` next to new logic.
- Branch: `dev` is where work lands; `main` is the release branch.
- Route handlers are plain `def` on purpose (SQLite calls block); keep it that way.
- `magic.py` is the only auth: never add an open path besides `/magic` and `/static/`. Client IP comes from `CF-Connecting-IP`.
- Speech uses the browser's speechSynthesis, preferring an fr-FR voice (`templates/base.html`).

## House rules (all my repos)
- Never use the em dash character. Use a period, comma, colon or parentheses. En dash only for number ranges.
- No AI attribution anywhere: no Co-Authored-By trailers, no "generated with" footers, no mentions in code, commits, PRs or docs.
- Conventional commits, lowercase, say why: `feat: ...`, `fix: ...`, `chore: ...`, `docs: ...`. One logical change per commit. Commit only when asked.
- Write the least code that works and reads like the code around it. No new dependency for what a few lines do. Non-trivial logic leaves one small runnable check behind.
- Never commit secrets, `.env`, databases or `backups/`. Secrets live in `~/sources/gen0/secrets` (sops) and reach the app as env vars.

## How it ships
Runs on my k0s homelab, defined in `~/sources/gen0` (see its AGENTS.md):
1. Commit here.
2. In gen0, set `appVersion` in `k0s/charts/ouioui/Chart.yaml` to that commit.
3. `make image-ouioui` (builds that commit in a clean worktree, pushes to registry.efk.sh).
4. `make diff`, then `helmfile -l name=ouioui sync` (with `KUBECONFIG=~/.kube/homelab`), commit gen0.

In the cluster: uid 1000, no Kubernetes API token, network policy blocks the LAN, tailnet and other pods. App data is the `/data` volume (`/srv/ouioui` on node k0s-1); everything else in the container is disposable.
