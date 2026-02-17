FROM python:3.11-slim

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install Chromium system dependencies required by Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libgbm1 \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libfontconfig1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxshmfence1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for layer caching.
# Copy pyproject.toml and create minimal package structure so pip can resolve deps.
COPY pyproject.toml .
RUN mkdir -p src config && \
    touch src/__init__.py config/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src config

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy full application code and install the package
COPY . .
RUN pip install --no-cache-dir .

# Create data directories (will be overlaid by Railway volume)
RUN mkdir -p /app/data/browser_data /app/data/deliverables /app/data/logs

CMD ["python", "main.py", "--headless"]
