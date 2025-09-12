# Use a build stage with necessary build dependencies
FROM python:3.11.11-bullseye as builder

# Install build packages only needed during build
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ gcc make git build-essential ca-certificates curl \
    libc-dev libssl-dev libffi-dev zlib1g-dev python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency spec first for better layer caching
COPY ./requirements.txt ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY . .

# Install the package (editable install keeps source for dev, switch to --no-deps . for prod)
RUN pip install --no-cache-dir .

# Create non-root user and set permissions (after install so site-packages owned by root, but app src owned by user)
RUN adduser --disabled-password --gecos '' laisky && \
    chown -R laisky:laisky /app
USER laisky

EXPOSE 8080

CMD ["python", "-m", "knowledge_storm.server"]
