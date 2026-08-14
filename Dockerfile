FROM python:3.13-slim

LABEL org.opencontainers.image.title="Jobs Portal" \
      org.opencontainers.image.description="Read/write web portal over a folder of job position notes"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JOBS_DIR=/data \
    PORT=8080 \
    HOST=0.0.0.0

# Set to 1 to bake LibreOffice in so the portal can render letters to PDF.
# It adds roughly 700MB, which is why the default image leaves it out.
ARG WITH_PDF=0

WORKDIR /app

RUN if [ "$WITH_PDF" = "1" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# Only dependency is the markdown renderer; without it the app falls back to a
# built-in subset renderer, so the image still works if this layer is stripped.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ ./templates/

EXPOSE 8080

# The notes live on a bind mount, so the container writes as whatever uid it is
# given. Override with `user:` in compose to match the host owner of the folder.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
