FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Бот работает не от root; данные (сессия MAX, БД, rules.json) — в томе /data.
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app
VOLUME ["/data"]

CMD ["python", "-m", "bot"]
