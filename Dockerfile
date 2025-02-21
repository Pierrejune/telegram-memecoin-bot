# Utiliser l'image officielle Python 3.9 slim
FROM python:3.9-slim

# Définir le répertoire de travail dans le conteneur
WORKDIR /app

# Copier les fichiers requirements.txt et bot.py dans le conteneur
COPY requirements.txt /app/
COPY bot.py /app/

# Installer les dépendances nécessaires
RUN pip install --no-cache-dir -r requirements.txt

# Installer python-dotenv pour charger les variables d'environnement depuis un fichier .env (facultatif)
RUN pip install python-dotenv

# Exposer le port du conteneur (défini via variable d'environnement)
EXPOSE ${PORT:-8080}

# Lancer l'application
CMD ["python", "bot.py"]
