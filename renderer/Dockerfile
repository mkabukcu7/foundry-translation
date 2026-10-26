# SPDX-License-Identifier: MIT
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home renderer
COPY app.py .
USER renderer
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
