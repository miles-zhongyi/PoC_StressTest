FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY dashboard/server.py /app/server.py
COPY dashboard/static/ /app/static/

EXPOSE 9090
CMD ["python", "server.py"]
