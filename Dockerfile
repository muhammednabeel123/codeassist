FROM python:3.11-slim

# tesseract = OCR engine; poppler-utils = PDF rasterisation for pdf2image.
# eng traineddata ships with tesseract-ocr; add tesseract-ocr-<lang> for others.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY samples ./samples

ENV CA_STORAGE_DIR=/data/storage \
    CA_DATABASE_URL=sqlite:////data/codeassist.db \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

# Run as a non-root user; PHI-bearing containers should never run as root.
RUN useradd -m -u 10001 coder && mkdir -p /data && chown -R coder /data /app
USER coder

EXPOSE 8000
WORKDIR /app/backend
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
