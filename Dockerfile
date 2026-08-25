FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Update yt-dlp
RUN pip install -U yt-dlp

# Install system packages
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    libopus-dev \
    nodejs \
    npm \
    && npm install -g @distube/yt-dlp \
    && rm -rf /var/lib/apt/lists/*

# Copy bot files + cookies.txt
COPY . .

CMD ["python", "main.py"]
