FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY common/ /app/common/
COPY ru/ru_server.py /app/ru_server.py

CMD ["python", "ru_server.py"]
