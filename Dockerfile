FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telefeed_clone.py .

RUN mkdir -p /app/data
VOLUME /app/data

CMD ["python", "-u", "telefeed_clone.py"]
