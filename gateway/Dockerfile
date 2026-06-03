FROM python:3.12-slim

WORKDIR /app

# build deps for lxml/onnxruntime wheels that occasionally compile on arm64
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 3001
CMD ["python", "app.py"]
