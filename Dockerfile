FROM python:3.13-slim

WORKDIR /app

# Install system dependencies if needed (e.g. for building some python packages)
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential \
#     && rm -rf /var/lib/apt/lists/*

COPY src/Networkbalancerservice src/Networkbalancerservice
COPY pyproject.toml ./
COPY README.md ./

RUN pip install --no-cache-dir ./

ENTRYPOINT ["python3", "src/Networkbalancerservice/networkbalancerservice.py"]