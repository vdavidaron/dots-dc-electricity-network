FROM python:3.13

RUN mkdir /app/
WORKDIR /app

COPY src/Networkbalancerservice src/Networkbalancerservice
COPY pyproject.toml ./
COPY README.md ./
RUN pip install ./

ENTRYPOINT python3 src/Networkbalancerservice/networkbalancerservice.py