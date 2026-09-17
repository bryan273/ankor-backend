# Anker Care Agent — container image.
#
# Two-stage so the runtime image carries no compiler: psycopg and Pillow both build
# native wheels, and shipping gcc to production roughly doubles the image for nothing.
#
# The base is slim rather than alpine deliberately — alpine uses musl, which has no
# manylinux wheels, so every dependency would compile from source and the build would go
# from ~1 minute to ~10.

FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim

# libpq5 is the runtime half of libpq-dev; without it psycopg imports and then fails to
# connect, which looks like a network problem rather than a missing library.
RUN apt-get update \
 && apt-get install --no-install-recommends -y libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
COPY . .

# Run as a non-root user. Cloud Run does not require it, but a container that only ever
# needed to read its own code has no reason to be able to write to it.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# `/livez`, NOT `/healthz`.
#
# `/healthz` pings the LLM, the embedding API, Pinecone and Postgres for real — that is
# what makes it useful to a human, and what makes it a terrible thing to call on a timer.
# Every 30 seconds is ~2,880 embedding requests a day spent proving the app is alive,
# against the same single free-tier key the corpus is embedded with, and this project has
# already had a Google account banned for request volume. `/livez` answers from the
# process itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/livez" || exit 1

CMD ["python", "run.py"]
