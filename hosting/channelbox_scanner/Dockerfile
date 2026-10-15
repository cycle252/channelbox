FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 ffmpeg tzdata
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version=$BUILD_VERSION io.hass.type="app" io.hass.arch=$BUILD_ARCH
WORKDIR /app
COPY scanner.py channelbox_feed.py numbers.json ./
CMD ["python3", "/app/scanner.py"]
