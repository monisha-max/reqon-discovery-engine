FROM python:3.11-slim

WORKDIR /app

# System dependencies for Playwright Chromium + image processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl \
    libnss3 libatk-bridge2.0-0 libdrm2 \
    libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libxshmfence1 libglib2.0-0 \
    libdbus-1-3 libx11-xcb1 libxcb1 \
    libxext6 libxfixes3 libxi6 \
    libgtk-3-0 libxtst6 \
    fonts-liberation fonts-noto-color-emoji \
    libjpeg62-turbo libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium
RUN python -m playwright install chromium \
    && python -m playwright install-deps chromium 2>/dev/null || true

# Application code
COPY . .

# Output directories
RUN mkdir -p output/screenshots output/auth output/models \
    output/training_data output/perf_reports output/defect_reports

# Verify axe-core bundled (download if missing)
RUN test -f layer5_defect_detection/assets/axe.min.js \
    || (mkdir -p layer5_defect_detection/assets \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js', 'layer5_defect_detection/assets/axe.min.js')")

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8765/ || exit 1

CMD ["python", "run_server.py"]
