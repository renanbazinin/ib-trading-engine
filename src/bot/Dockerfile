FROM python:3.12-slim

WORKDIR /app

# Ensure we have required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Copy the official wheel and install it directly
COPY ibapi-*.whl .
RUN pip install --no-cache-dir ./ibapi-10.35.1-py3-none-any.whl

# Install the rest of the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the TWS bot codebase
COPY . .

# Dashboard port (actual default is 5050 via DASHBOARD_PORT env)
EXPOSE 5050

# Run entrypoint
CMD ["python", "main.py"]