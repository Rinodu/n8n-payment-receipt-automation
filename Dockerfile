FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home receipt
COPY processor.py gateway.py storage.py admin.py migrate_legacy.py ./
RUN mkdir /data && chown receipt:receipt /data
USER receipt
ENV DATA_DIR=/data BIND_HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8787
CMD ["python", "processor.py"]
