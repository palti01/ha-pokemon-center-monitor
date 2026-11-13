FROM python:3.13-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Berlin
WORKDIR /app
RUN apk add --no-cache tzdata && pip install --no-cache-dir paho-mqtt==2.1.0
COPY xenia_bridge.py .
CMD ["python", "xenia_bridge.py"]
