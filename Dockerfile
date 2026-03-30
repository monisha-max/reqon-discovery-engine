FROM python:3.11-slim

WORKDIR /app

# System dependencies for Playwright + Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libxshmfence1 \
    libglib2.0-0 \
    libdbus-1-3 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (install first for Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium browser
RUN python -m playwright install chromium \
    && python -m playwright install-deps chromium

# Application code
COPY . .

# Ensure output directories exist
RUN mkdir -p output/screenshots output/auth output/models output/training_data \
    output/perf_reports output/defect_reports

# Verify axe-core is bundled
RUN test -f layer5_defect_detection/assets/axe.min.js \
    || (echo "axe.min.js missing — downloading..." \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js', 'layer5_defect_detection/assets/axe.min.js')")

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/')" || exit 1

CMD ["python", "run_server.py"]
