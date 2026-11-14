FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends tzdata libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY streakbot ./streakbot
RUN pip install . && playwright install --with-deps firefox \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home streakbot && mkdir -p /app/data && chown streakbot:streakbot /app/data
USER streakbot

EXPOSE 8080

CMD ["python", "-m", "streakbot"]
