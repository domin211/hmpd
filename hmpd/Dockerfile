ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/ /app/

RUN python3 -m venv /opt/venv     && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt     && chmod +x /app/run.sh

ENV PATH="/opt/venv/bin:${PATH}"     PYTHONUNBUFFERED=1

CMD ["/app/run.sh"]
