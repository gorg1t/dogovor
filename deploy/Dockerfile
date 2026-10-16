# Один образ на бота, воркер и API (контекст сборки — корень репозитория)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 TZ=Europe/Moscow
WORKDIR /app
COPY backend/requirements-prod.txt .
RUN pip install -r requirements-prod.txt
COPY backend/dogovorilis ./dogovorilis
# корень Минцифры для TLS GigaChat; config.py ищет его в <корень>/certs
COPY certs /certs
