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

CMD ["sh", "-c", "alembic upgrade head || echo 'Migration failed, starting anyway'; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
