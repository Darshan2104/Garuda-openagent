# Build from the repository root for GARUDA_LIVE_OWNED_DOCKER=1.
FROM python:3.12-slim
COPY garuda /opt/garuda/garuda
ENV PYTHONPATH=/opt/garuda
RUN chmod -R a+rX /opt/garuda
