FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY common/ /app/common/
COPY du/du_server.py /app/du_server.py

CMD ["python", "du_server.py"]
