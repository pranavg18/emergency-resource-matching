# Build from this directory: docker build -t storage-node ./storage_node
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/storage_node

# Non-root. On Kubernetes, set securityContext.fsGroup: 10001 so the
# mounted volume is writable by this user.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080

CMD ["gunicorn", "-c", "storage_node/gunicorn.conf.py", "storage_node.app:create_app()"]
