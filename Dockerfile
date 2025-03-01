# Utiliser une image Python 3.9 slim
FROM python:3.9-slim

# Définir le répertoire de travail
WORKDIR /app

# Copier les fichiers nécessaires
COPY requirements.txt /app/
COPY bot.py /app/

# Installer les dépendances système et Python
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Exposer le port
EXPOSE 8080

# Commande pour lancer le bot avec Waitress
CMD ["python", "bot.py"]
