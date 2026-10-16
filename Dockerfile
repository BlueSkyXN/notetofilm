FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /svc/data /svc/config && chown appuser:appuser /svc/data /svc/config

WORKDIR /svc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY vendor/ vendor/
COPY tests/ tests/
COPY scripts/check_tests.py scripts/check_tests.py
COPY config.example.toml config.example.toml

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser
USER 1000:1000
RUN PYTHONPATH=/svc python -B scripts/check_tests.py
EXPOSE 8765
CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8765", "--no-proxy-headers"]
