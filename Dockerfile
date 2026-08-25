FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install -U yt-dlp[default]

RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    libopus-dev \
    nodejs \
    npm \
    && npm install -g @distube/ytdl-core \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "main.py"]
