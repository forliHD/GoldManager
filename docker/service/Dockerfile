# Shared Python service image for all five services.
# The actual service is selected at runtime via the SERVICE_ROLE env var,
# which the entrypoint dispatches on. This keeps build cache warm and
# lets us add a new service by adding one more case to the dispatcher.
#
# Build context is the repo root so we can `pip install -e .` and get
# the local package plus its declared dependencies.

FROM python:3.11-slim AS base

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: build-essential for any wheels, libstdc++ for pandas, curl for healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the package and dev extras first (changes less often than src/).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[dev]"

# Runtime prompt for the AI decision layer (OpenRouterClient reads
# ./decision_agent.md by default). Harmless when the AI layer is off.
COPY decision_agent.md ./

# Image-level liveness: the interpreter can import the package. The
# compose healthcheck does the deeper, role-aware heartbeat check
# (python -m xauusd_bot.healthcheck).
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import xauusd_bot" || exit 1

# Dispatcher: SERVICE_ROLE picks the actual entry point. Default to the
# smoke CLI for dev-friendly behavior.
RUN mkdir -p /app/logs /app/data
ENTRYPOINT ["python", "-m", "xauusd_bot.docker_entrypoint"]
