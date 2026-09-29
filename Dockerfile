FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Browsers live outside the wheel so an API-only restart never re-downloads them.
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend

# `--with-deps` is what makes this an *executor* image: the pinned Playwright range resolves to one
# Chromium revision, and the executor refuses a channel it cannot find (§15.1).
RUN pip install . \
    && python -m playwright install --with-deps chromium \
    && rm -rf ./backend

EXPOSE 8000
# `python -m app.main` rather than a bare uvicorn call: API_HOST/API_PORT are read, not overridden.
CMD ["python", "-m", "app.main"]
