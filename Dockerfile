# Use Python 3.11 (has audioop built-in)
FROM python:3.11-slim

# Install system deps (for moviepy, ffmpeg, etc.)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working dir
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose port (for Flask keep-alive)
EXPOSE 10000

# Start bot
CMD ["python", "main.py"]