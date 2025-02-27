FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt /app/
COPY bot.py /app/
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE ${PORT:-8080}
CMD ["python", "bot.py"]
