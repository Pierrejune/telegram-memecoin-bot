FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt /app/
COPY bot.py /app/
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y gunicorn || true
EXPOSE 8080
CMD ["python", "bot.py"]
