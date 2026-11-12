FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# profiles.toml + .env are mounted to /config, the database lives in /data
VOLUME ["/config", "/data"]
CMD ["python", "-m", "flip_scout", "--config", "/config/profiles.toml", "--env", "/config/.env", \
     "--data", "/data", "--log-file", "/data/flip-scout.log"]
