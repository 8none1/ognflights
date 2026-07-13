# ognflights collector + dashboard. Stdlib only, so no pip install step.
FROM python:3.12-slim

# git + ssh let the hourly publish worker push the public-data branch (no pip deps).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ognflights/ ./ognflights/
COPY cli.py ./
COPY replay/ ./replay/
COPY publish/ ./publish/

# SQLite year files (data/ogn-YYYY.sqlite) + the DDB cache live on a mounted volume.
VOLUME ["/app/data"]
EXPOSE 8080
ENV PYTHONUNBUFFERED=1

# Prove the whole chain is healthy, not just that the process exists: the check hits the
# local /healthz, which is 200 only when the web server is up AND the collector's link to
# the OGN backend (APRS-IS) is live. A quiet sky (no aircraft in range) stays healthy.
# start-period + retries tolerate a normal reconnect (backoff maxes at 60s) without flapping.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python3", "cli.py", "healthcheck", "--port", "8080"]

# Follow aircraft that launch from the field, and serve the replay + /stats page.
CMD ["python3", "cli.py", "watch", "--serve", "--port", "8080"]
