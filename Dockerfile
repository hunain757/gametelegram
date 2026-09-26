FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_FILE=/data/data.json

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
COPY static ./static

# Mount a volume here so users, trades and stats survive restarts and redeploys.
VOLUME /data

CMD ["python", "bot.py"]
