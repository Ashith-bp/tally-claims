FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data
EXPOSE 8000

# One worker + threads: SQLite is happiest with a single writer process.
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 --timeout 60 -b 0.0.0.0:${PORT:-8000} app:app"]
