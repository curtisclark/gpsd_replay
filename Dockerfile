FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY gpsd_replay/ ./gpsd_replay/

RUN pip install --no-cache-dir .

# gpsd TCP port, Web UI port
EXPOSE 2947 8765

ENTRYPOINT ["gpsd-replay"]
