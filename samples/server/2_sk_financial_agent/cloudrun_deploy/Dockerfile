# ─────────────────────────────────────────────────────────────
# Stage 1 – builder: installs Python deps in an isolated layer
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY wheels/stls_a2a-0.1.0-py3-none-any.whl .
RUN pip install --prefix=/install --no-cache-dir \
        stls_a2a-0.1.0-py3-none-any.whl

COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────────────────────
# Stage 2 – runtime: Python 3.12 + Eclipse Temurin 21 JRE
# openjdk-21 non e' nel mirror apt default di bookworm-slim;
# si usa il repo ufficiale Adoptium.
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg apt-transport-https ca-certificates \
    && wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public \
       | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" \
       > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        temurin-21-jre \
    && rm -rf /var/lib/apt/lists/*

RUN java -version

ENV JAVA_HOME=/usr/lib/jvm/temurin-21-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

WORKDIR /app

COPY --from=builder /install /usr/local

COPY app/ .

# STLS Java SDK
COPY jars/stls-ai-0.0.1.jar /opt/stls/lib/stls-ai-0.0.1.jar

# Cloud Run contract
ENV PORT=8080
ENV CLASSPATH=/opt/stls/lib/stls-ai-0.0.1.jar

RUN useradd -m -u 1001 appuser && chown -R appuser /app
USER appuser

CMD ["python", "server.py"]