FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000 DEMO_DATA_DIR=/data
WORKDIR /app
RUN mkdir /data && chown 10001:10001 /data
COPY server.py demo.py workflow.py policies.py ./
COPY public/ ./public/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/healthz', timeout=3).close()"]
CMD ["python", "server.py"]
