FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WEBCLIPPER_DATA_DIR=/data

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY templates /app/templates

RUN mkdir -p /data /data/clips /data/thumbnails /data/preview

EXPOSE 9069

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9069"]
