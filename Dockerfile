FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/app
RUN groupadd --gid 10001 releaseguard && useradd --uid 10001 --gid 10001 --no-create-home releaseguard
COPY checkout.py ./

FROM base AS checkout
USER 10001:10001
EXPOSE 8001
CMD ["python", "checkout.py"]

FROM base AS controller
COPY deployment/requirements.txt /tmp/controller-requirements.txt
RUN pip install --no-cache-dir -r /tmp/controller-requirements.txt && install -d -o 10001 -g 10001 /data
COPY *.py ./
COPY deployment/runtime.py ./deployment/runtime.py
COPY web/ ./web/
USER 10001:10001
EXPOSE 8787
CMD ["python", "hosted.py"]
