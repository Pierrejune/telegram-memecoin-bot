FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
COPY bot.py .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8080
USER 1000  # Sécurité : ne pas exécuter en root
CMD ["python", "bot.py"]
