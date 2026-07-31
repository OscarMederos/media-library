FROM python:3.11-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app
COPY static /static
COPY media/sq.js /static/sq.js
ENV DB_PATH=/data/media.db
EXPOSE 8000

RUN mkdir -p /cert &&     openssl req -x509 -nodes -days 365 -newkey rsa:2048     -keyout /cert/key.pem -out /cert/cert.pem     -subj "/CN=localhost"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--ssl-keyfile", "/cert/key.pem", "--ssl-certfile", "/cert/cert.pem"]
