FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN adduser --disabled-password --gecos "" mcpuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py database.py server.py ./
COPY providers/ ./providers/

RUN chown -R mcpuser:mcpuser /app
USER mcpuser

EXPOSE 8080

CMD ["python", "server.py"]
