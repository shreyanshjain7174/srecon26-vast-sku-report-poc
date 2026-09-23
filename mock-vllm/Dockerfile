FROM python:3.13.7-slim@sha256:5f55cdf0c5d9dc1a415637a5ccc4a9e18663ad203673173b8cda8f8dcacef689
WORKDIR /app
COPY server.py /app/server.py
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["python", "/app/server.py"]
