FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home appuser
USER appuser
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "ev_fleet.fleet.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
