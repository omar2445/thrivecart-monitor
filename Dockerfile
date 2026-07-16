FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# main.py reads PORT from the environment (defaults to 8000),
# so no shell expansion is needed — works on Railway and docker-compose alike.
CMD ["python", "main.py"]
