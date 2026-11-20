# ---- PDF.js viewer (static files, downloaded at build time; legacy build = older iOS/Android too)
FROM python:3.12-slim AS pdfjs
ARG PDFJS_VERSION=6.5.438
RUN python -c "import io, sys, urllib.request, zipfile; \
v = sys.argv[1]; \
url = f'https://github.com/mozilla/pdf.js/releases/download/v{v}/pdfjs-{v}-legacy-dist.zip'; \
zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url).read())).extractall('/pdfjs')" "$PDFJS_VERSION" \
 && find /pdfjs -name '*.map' -delete \
 && rm -f /pdfjs/web/compressed.tracemonkey-pldi-09.pdf

FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-hun tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=pdfjs /pdfjs /srv/pdfjs
COPY app ./app

# ---- test image: app + pytest (build with --target test, see tests/run.sh)
FROM app AS test
COPY tests/requirements.txt /tmp/test-requirements.txt
RUN pip install --no-cache-dir -r /tmp/test-requirements.txt
ENV DATA_DIR=/tmp/pdfm-test-data
CMD ["pytest", "-q", "tests"]

# ---- runtime image (default target)
FROM app AS runtime
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
