FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Install exactly what uv.lock pins (no dev tools), in a layer cached separately from the code.
RUN pip install --no-cache-dir uv==0.13.0
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
ENV PATH="/app/.venv/bin:$PATH"

# One worker on purpose: conversation state is in process memory (see app/sessions.py).
# No --proxy-headers: the app parses X-Forwarded-For itself with a fixed trusted-hop count (app/main.py:client_ip).
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
