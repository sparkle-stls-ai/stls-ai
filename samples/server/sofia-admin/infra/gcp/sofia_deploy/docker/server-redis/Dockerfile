# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile — server-redis
#
# Build context: directory del server nel repo applicativo
#   server-redis/
#   ├── wheels/
#   │   └── stls_a2a-X.Y.Z-py3-none-any.whl
#   ├── requirements.txt
#   └── app/
#       ├── server.py
#       └── agent_redis/
#
# KEY_PROVIDER_NAME=base → nessun JVM/Java richiesto
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Wheel stls_a2a (qualsiasi versione presente in wheels/)
COPY wheels/stls_a2a-*.whl ./
RUN pip install --prefix=/install --no-cache-dir stls_a2a-*.whl

# Dipendenze applicative
COPY requirements.txt ./
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# Pacchetti installati nello stage builder
COPY --from=builder /install /usr/local

# Tutti i sorgenti del server (server.py + moduli agente + eventuali altri file)
COPY app/ .

ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV KEY_PROVIDER_NAME=base

RUN useradd -m -u 1001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

CMD ["python", "server.py"]