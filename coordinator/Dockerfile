# Build from this directory: docker build -t coordinator ./coordinator
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/coordinator

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

CMD ["gunicorn", "-c", "coordinator/gunicorn.conf.py", "coordinator.app:create_app()"]
