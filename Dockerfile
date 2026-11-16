FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu-core curl \
 && rm -rf /var/lib/apt/lists/*

# Bold display font for hooks/captions; "Impact" in config maps to it on Linux
RUN mkdir -p /usr/share/fonts/truetype/anton \
 && (curl -fsSL -o /usr/share/fonts/truetype/anton/Anton-Regular.ttf \
      https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf || true) \
 && printf '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig><alias><family>Impact</family><prefer><family>Anton</family><family>DejaVu Sans</family></prefer></alias></fontconfig>' > /etc/fonts/local.conf \
 && fc-cache -f

WORKDIR /app
COPY requirements.txt requirements-server.txt ./
# piper-tts (free voiceover) is optional: if no wheel exists for this CPU, the agent still builds and runs
RUN grep -v piper-tts requirements.txt > /tmp/req-core.txt \
 && pip install --no-cache-dir -r /tmp/req-core.txt -r requirements-server.txt \
 && (pip install --no-cache-dir "piper-tts>=1.3" || echo "piper-tts unavailable on this CPU; voiceover can use ElevenLabs/OpenAI")
COPY . .

ENV REELAGENT_HOME=/data PYTHONUNBUFFERED=1 HOST=127.0.0.1 PORT=8080
CMD ["python", "-m", "reelagent", "serve"]
