FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt /app/
COPY bot.py /app/
# Ajout des dépendances système pour compiler des packages comme solders
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    && pip install --no-cache-dir -r requirements.txt
EXPOSE ${PORT:-8080}
CMD ["python", "bot.py"]
