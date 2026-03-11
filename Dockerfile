FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WEBCLIPPER_DATA_DIR=/data

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates

RUN mkdir -p /data /data/clips /data/thumbnails /data/preview

EXPOSE 9069

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9069"]
