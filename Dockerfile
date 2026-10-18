# ParseFlow on a cloud server: one container, data (documents + accounts database) on a persistent
# disk mounted at /data. Build:  docker build -t parseflow .
# Run:   docker run -p 8000:8000 -v parseflow-data:/data parseflow
FROM python:3.12-slim

# Tesseract for scanned pages; LibreOffice for the best layout of Word/PowerPoint and old .doc/.ppt/.xls
# files (set --build-arg WITH_LIBREOFFICE=0 for a ~600 MB smaller image: DOCX/PPTX/XLSX still open).
ARG WITH_LIBREOFFICE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng fonts-dejavu-core poppler-utils \
 && if [ "$WITH_LIBREOFFICE" = "1" ]; then apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui; fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
COPY .env.example ./.env.example

ENV PARSEFLOW_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=1 \
    JOB_WORKERS=1 \
    PARSEFLOW_OFFICE=builtin
RUN useradd --create-home parseflow && mkdir -p /data && chown parseflow /data
USER parseflow
EXPOSE 8000
WORKDIR /app/backend
# --proxy-headers: behind the host's HTTPS proxy, cookies are then marked Secure correctly
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
