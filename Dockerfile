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

# This host has no GPU. Install CPU-only torch/torchvision FIRST, from PyTorch's CPU
# wheel index, so the transitive deps below (easyocr, ultralytics) reuse them instead
# of pulling the multi-GB CUDA build. Saves ~2-3 GB of image size and a large chunk of
# RAM at model-load time (no CUDA libraries mapped in).
RUN pip install --no-cache-dir torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the easyocr English models at build time so they are baked into the
# image: no first-request download, and they survive container restarts (the cache
# at /root/.EasyOCR is otherwise ephemeral and re-downloaded after every restart).
RUN python -c "import easyocr; easyocr.Reader(['en'], gpu=False, verbose=False)"

COPY pipeline ./pipeline
COPY webapp ./webapp
COPY scripts ./scripts
COPY models ./models
COPY main.py .

EXPOSE 8000
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
