# syntax=docker/dockerfile:1
FROM node:26-bookworm-slim AS node
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WXAPP_TOOL_ROOT=/opt/wxappUnpacker \
    WECHAT_FRIDA=frida

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       adb ca-certificates git libatomic1 tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY version_scraper/requirements.txt /tmp/requirements.txt
RUN pip install --requirement /tmp/requirements.txt

# Keep the upstream source, commit and GPL license with the executable tool.
RUN git clone https://github.com/threecha/wxappUnpacker.git /opt/wxappUnpacker \
    && git -C /opt/wxappUnpacker checkout --detach 694ca3874e67b89dd709f3d577c85a553bd45a0c \
    && git config --system --add safe.directory /opt/wxappUnpacker \
    && cd /opt/wxappUnpacker \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

COPY docker/requirements.txt /tmp/docker-requirements.txt
RUN pip install --requirement /tmp/docker-requirements.txt

WORKDIR /opt/wechat
COPY LICENSE README.md CITATION.cff ./
COPY docs ./docs
COPY MiniZooCollectionCore ./MiniZooCollectionCore
COPY scraper ./scraper
COPY version_scraper ./version_scraper
COPY docker ./docker
RUN useradd --create-home --uid 1000 collector \
    && mkdir -p /data /input /config \
    && chmod -R a+rX /opt/wechat /opt/wxappUnpacker \
    && chown 1000:1000 /data
USER 1000:1000
ENTRYPOINT ["python3", "/opt/wechat/docker/entrypoint.py"]
CMD ["help"]
