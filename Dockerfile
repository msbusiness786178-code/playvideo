# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Non-root user for security
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY --chown=app:app . .

USER app

# ── Runtime ─────────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000

# Gunicorn: 2 workers × 4 threads each — good for Render free tier (512 MB RAM)
# --worker-class=gthread lets streaming responses (iter_content) work properly
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--keep-alive", "5", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
