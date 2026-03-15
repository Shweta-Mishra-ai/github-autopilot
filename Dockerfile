FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for SQLite and ChromaDB
RUN mkdir -p data/chroma

# Default command â€” web server
# Override with: docker run ... python worker.py
CMD ["gunicorn", "server:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120"]
