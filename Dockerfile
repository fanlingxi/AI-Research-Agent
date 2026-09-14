FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements-runtime.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements-runtime.txt
COPY app ./app
COPY main.py ./main.py

EXPOSE 8000
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
