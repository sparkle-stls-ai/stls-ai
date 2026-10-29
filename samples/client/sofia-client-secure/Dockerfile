# ─── Stage 1: Build Node.js ────────────────────────────────────────────────
FROM node:20-alpine AS node-build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# NOTE: VITE_AUTH_MODE=none means no login at all — anyone reaching the URL can
# use every configured agent. Use it only behind another auth layer / internal network.
ARG VITE_MODE=production
ARG VITE_AUTH_MODE=none
ARG VITE_BASE=/
ENV VITE_MODE=$VITE_MODE
ENV VITE_AUTH_MODE=$VITE_AUTH_MODE
ENV VITE_BASE=$VITE_BASE

RUN npm run build:all

# ─── Stage 2: Build liboqs (post-quantum KEM C library) ───────────────────
# Built from source against the SAME base image as the runtime stage below,
# so the resulting .so is guaranteed musl-ABI compatible (Alpine has no
# prebuilt liboqs package). Kept in its own stage so the build toolchain
# (cmake/ninja/gcc/...) never ends up in the final runtime image.
FROM node:20-alpine AS liboqs-build
RUN apk add --no-cache cmake ninja gcc g++ make musl-dev linux-headers git python3 openssl-dev

# Must match the liboqs-python version resolved via stls_a2a[pqc] in
# python/requirements.txt (see comment there) - a mismatch between the C
# library and the ctypes wrapper causes AttributeError at runtime, not at
# build/install time.
ARG LIBOQS_VERSION=0.14.0

RUN git clone --branch "$LIBOQS_VERSION" --depth 1 \
      https://github.com/open-quantum-safe/liboqs.git /tmp/liboqs && \
    cmake -S /tmp/liboqs -B /tmp/liboqs/build -GNinja \
      -DCMAKE_INSTALL_PREFIX=/opt/liboqs \
      -DCMAKE_BUILD_TYPE=Release \
      -DOQS_BUILD_ONLY_LIB=ON \
      -DBUILD_SHARED_LIBS=ON && \
    ninja -C /tmp/liboqs/build install && \
    rm -rf /tmp/liboqs

# ─── Stage 3: Runtime (Node + Python) ──────────────────────────────────────
FROM node:20-alpine AS runtime
WORKDIR /app

# ── Python 3 + pip ────────────────────────────────────────────────────────
RUN apk add --no-cache python3 py3-pip

# ── liboqs C library (post-quantum KEM, used when KEY_PROVIDER_NAME=pqc) ──
# Shared object + headers from the liboqs-build stage above. Only needed at
# runtime; the ctypes wrapper (liboqs-python) locates it via LD_LIBRARY_PATH
# since musl has no ldconfig cache like glibc does.
COPY --from=liboqs-build /opt/liboqs/lib/ /usr/local/lib/
COPY --from=liboqs-build /opt/liboqs/include/ /usr/local/include/
ENV LD_LIBRARY_PATH=/usr/local/lib

# ── Python dependencies + stls_a2a / stls_core wheels ──────────────────────
# python/wheels/ contains the pre-built stls_a2a/stls_core wheels bundled
# with the repo. pip is run from /app/python so the relative path
# ./wheels/... resolves correctly. The stls_a2a[pqc] extra additionally
# pulls liboqs-python from PyPI (network access required at build time).
COPY python/ ./python/
RUN pip install --break-system-packages --no-cache-dir -r /app/python/requirements.txt \
    --find-links /app/python/wheels

# ── Node production deps ───────────────────────────────────────────────────
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# ── Artefacts from the build stage ────────────────────────────────────────
COPY --from=node-build /app/dist ./dist
COPY --from=node-build /app/server/dist ./server/dist

# ── Java runtime (needed only when KEY_PROVIDER_NAME=arqit or simple) ─────
# Uncomment the two lines below and supply the JAR if required:
# RUN apk add --no-cache openjdk17-jre
# COPY path/to/stls-ai-0.0.1.jar /app/stls-ai.jar
# ENV CLASSPATH=/app/stls-ai.jar

# ── Startup script ─────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080
ENV PORT=8080
ENV NODE_ENV=production
ENV SECURE_TRANSPORT_HOST=0.0.0.0
ENV SECURE_TRANSPORT_PORT=8090
ENV SECURE_TRANSPORT_URL=http://localhost:8090

ARG VITE_MODE=production
ARG VITE_AUTH_MODE=none
ENV VITE_MODE=$VITE_MODE
ENV VITE_AUTH_MODE=$VITE_AUTH_MODE

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s \
  CMD wget -qO- "http://localhost:$PORT${BASE_PATH}/api/health" || exit 1

CMD ["/app/docker-entrypoint.sh"]
