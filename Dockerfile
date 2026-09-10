FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY artnet_monitor/ artnet_monitor/
# Only the shipped template, never a real config/patch.yaml -- that's
# per-installation data (real LAN IP, real patch) and belongs in the mounted
# volume, not baked into an image. config_bootstrap creates patch.yaml from
# this template on first run if the volume doesn't already have one.
COPY config/patch.yaml.default config/patch.yaml.default

EXPOSE 6454/udp
EXPOSE 8000/tcp

# --web-host 0.0.0.0 because the dashboard is useless bound to the
# container's own loopback; reach it via the host's published port. Pair
# with ARTNET_AUTH_TOKEN on any network you don't fully trust.
ENTRYPOINT ["python", "-m", "artnet_monitor.main", \
            "--config", "config/patch.yaml", "--web-host", "0.0.0.0"]
