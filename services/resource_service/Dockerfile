FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common common
COPY services services
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
# --keep-alive 75: longer than ingress-nginx keeps upstream connections (60 s),
# so gunicorn never closes one just as nginx sends on it (a 502 for POSTs).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--keep-alive", "75", "services.resource_service.app:create_app()"]
