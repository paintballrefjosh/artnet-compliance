FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY artnet_monitor/ artnet_monitor/
COPY config/ config/

EXPOSE 6454/udp
EXPOSE 8000/tcp

ENTRYPOINT ["python", "-m", "artnet_monitor.main", "--config", "config/patch.yaml"]
