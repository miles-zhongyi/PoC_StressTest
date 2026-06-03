FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY dashboard/server.py /app/server.py
COPY dashboard/trace_catalog.py /app/trace_catalog.py
COPY dashboard/static/ /app/static/
COPY common/ /app/common/
COPY CallFlow/record-id-messages.txt /app/CallFlow/record-id-messages.txt

EXPOSE 9090
CMD ["python", "server.py"]
