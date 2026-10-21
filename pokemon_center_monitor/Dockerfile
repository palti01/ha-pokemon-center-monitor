FROM python:3.13-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Berlin
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache tzdata && pip install --no-cache-dir -r requirements.txt
COPY pcmonitor ./pcmonitor
CMD ["python", "-m", "pcmonitor", "-c", "/nonexistent.yaml"]
