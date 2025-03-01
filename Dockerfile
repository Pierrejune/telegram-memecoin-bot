# Utiliser une image Python 3.9 slim comme base
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
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y gunicorn || true  # Supprimer Gunicorn s'il est présent, ignorer s'il n'est pas là

# Exposer le port (8080 pour Cloud Run)
EXPOSE 8080

# Commande pour lancer le bot avec Waitress
CMD ["python", "bot.py"]
