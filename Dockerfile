FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# tesseract и poppler нужны распознаванию сканов: маршрут-квитанции без
# текстового слоя и документы личности (MRZ паспортов и ID-карт).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project || uv sync --no-dev --no-install-project --no-cache

COPY . .

ENV PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# Keep the application UID/GID stable so bind-mounted media/static directories
# have predictable ownership on production hosts.
RUN addgroup --gid 1000 appuser \
    && adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
# web-сервис; job runner запускается тем же образом с командой run_jobs
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "60"]
