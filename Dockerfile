FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The application never needs root privileges at runtime.
RUN addgroup --system app \
    && adduser --system --disabled-password --no-create-home --ingroup app app \
    && chown app:app /app
COPY --chown=app:app . .
USER app

# Koyeb sets $PORT itself and routes traffic to it — app.py reads it.
EXPOSE 8000

CMD ["python", "app.py"]
