# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Create a non-root user
RUN useradd -m worker

# Install Meshroom dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Download and install Meshroom
RUN wget "https://github.com/alicevision/Meshroom/releases/download/v2023.3.0/Meshroom-2023.3.0-linux.tar.gz" -O /tmp/meshroom.tar.gz && \
    tar -zxvf /tmp/meshroom.tar.gz -C /opt && \
    rm /tmp/meshroom.tar.gz
ENV PATH="/opt/Meshroom-2023.3.0:${PATH}"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code
COPY . .

# Change ownership of the app directory
RUN chown -R worker:worker /app

# Switch to the non-root user
USER worker

# Command to run the Celery worker
CMD ["celery", "-A", "tasks", "worker", "--loglevel=info"]
