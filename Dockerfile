FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY evals/ evals/
COPY schemas/ schemas/

RUN pip install --no-cache-dir -e ".[notion]" \
    && playwright install --with-deps chromium

CMD ["python", "-m", "src.slack.app"]
