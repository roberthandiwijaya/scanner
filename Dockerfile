FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    MAX_UPLOAD_MB=512

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

EXPOSE 8000

CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "app:app"]
