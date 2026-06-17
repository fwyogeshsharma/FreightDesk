# One image, two roles: the web app (default CMD) and the extraction pipeline
# (overridden command — see the "pipeline" service in docker-compose.yml).
# Python 3.12 in-container for the widest ML-wheel availability (torch/easyocr),
# independent of the host's Python 3.14.
FROM python:3.12-slim

# OpenCV (headless) + easyocr need a couple of shared libs; ffmpeg for video decode.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline ./pipeline
COPY webapp ./webapp
COPY scripts ./scripts
COPY models ./models
COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
