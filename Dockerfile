# ognflights collector: buddy-follow OGN capture daemon.
# Stdlib only, so no pip install step; the image is just Python + our code.
FROM python:3.12-slim

WORKDIR /app
COPY ognflights/ ./ognflights/
COPY cli.py ./

# SQLite year files (data/ogn-YYYY.sqlite) + the DDB cache live on a mounted volume.
VOLUME ["/app/data"]
ENV PYTHONUNBUFFERED=1

# Follow aircraft that launch from the field, anywhere, until they land.
CMD ["python3", "cli.py", "watch"]
