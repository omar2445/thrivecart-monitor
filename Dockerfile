FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Shell form so $PORT is honored on platforms that set it (e.g. Railway);
# defaults to 8000 for self-hosting with docker-compose.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
