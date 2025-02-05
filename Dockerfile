# Use a build stage with necessary build dependencies
FROM python:3.11.11-bullseye as builder

# Install build packages only needed during build
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ gcc make git build-essential ca-certificates curl \
    libc-dev libssl-dev libffi-dev zlib1g-dev python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ./requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

# Build and install the package into a target directory
RUN python setup.py install --prefix=/install

# Create non-root user and set permissions
RUN adduser --disabled-password --gecos '' laisky && \
    chown -R laisky:laisky /app
USER laisky

EXPOSE 8080

CMD ["python", "-m", "knowledge_storm.server"]
