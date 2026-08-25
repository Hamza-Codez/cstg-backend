FROM python:3.12-slim

# uv manages dependencies and the virtualenv (see backend/pyproject.toml).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# The venv lives OUTSIDE /app on purpose: compose bind-mounts ./backend over /app
# for live reload, which would otherwise shadow the image's venv with the host's.
# Keeping it at /opt/venv means a rebuild always wins and no anonymous volume is
# needed to protect it (an anonymous volume would itself go stale on dep changes).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first so application edits do not invalidate it.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

EXPOSE 8000

# `${PORT:-8000}`, never a bare 8000. Railway (and Render, Fly, Cloud Run)
# inject PORT and route the public domain to THAT port; a container listening on
# a hardcoded 8000 is running perfectly and still unreachable, which the edge
# reports as "the train has not arrived at the station" — a message that says
# nothing about ports. The fallback keeps docker-compose, which sets no PORT,
# working unchanged.
#
# `|| echo` and not `&&`, deliberately (rules.md §3): the server starts even
# when the migration fails, so the failure is reachable at /health/db instead of
# an opaque 502 with no logs. With `&&` a bad DATABASE_URL means uvicorn never
# starts at all, and the only symptom is the edge 404 above.
CMD ["sh", "-c", "alembic upgrade head || echo 'Migration failed, starting anyway'; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
