FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY common/ /app/common/
COPY ue/ue_sim.py /app/ue_sim.py

CMD ["python", "ue_sim.py"]
