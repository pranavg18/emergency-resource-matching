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
CMD ["gunicorn", "-c", "services/matching_service/gunicorn.conf.py", "services.matching_service.app:create_app(start_background=True)"]
