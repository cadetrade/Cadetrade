# Server h24. Monta volumi persistenti su /app/state e /app/logs, e passa le chiavi come file o variabili d'ambiente.
FROM python:3.13-slim
ENV TZ=Europe/Rome PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm tzdata && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m pytest -q
VOLUME ["/app/state", "/app/logs"]
CMD ["python", "-m", "cadetrade", "schedule"]
