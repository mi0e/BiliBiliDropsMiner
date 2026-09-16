FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WEB_HOST=0.0.0.0 WEB_PORT=23333 WEB_DATA_DIR=/data
WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt \
    && useradd --create-home --uid 10001 miner \
    && mkdir /data && chown miner:miner /data
COPY bilibili_drops_miner/ bilibili_drops_miner/
COPY bilibili_web.py .
USER miner
EXPOSE 23333
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:23333/healthz', timeout=3)"
CMD ["python", "bilibili_web.py"]
