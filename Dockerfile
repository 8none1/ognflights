# ognflights collector + dashboard. Stdlib only, so no pip install step.
FROM python:3.12-slim

WORKDIR /app
COPY ognflights/ ./ognflights/
COPY cli.py ./
COPY replay/ ./replay/

# SQLite year files (data/ogn-YYYY.sqlite) + the DDB cache live on a mounted volume.
VOLUME ["/app/data"]
EXPOSE 8080
ENV PYTHONUNBUFFERED=1

# Follow aircraft that launch from the field, and serve the replay + /stats page.
CMD ["python3", "cli.py", "watch", "--serve", "--port", "8080"]
