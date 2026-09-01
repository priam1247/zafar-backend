FROM python:3.12-slim

WORKDIR /app

# System deps needed to build bcrypt/cryptography wheels on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Koyeb sets $PORT itself and routes traffic to it — app.py already reads
# that env var (falls back to 20208 only if PORT is unset, e.g. local runs).
EXPOSE 8000

CMD ["python", "app.py"]
