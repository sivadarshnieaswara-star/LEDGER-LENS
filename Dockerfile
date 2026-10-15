# Runs anywhere that takes a container (Fly.io, Railway, Cloud Run, a VPS).
FROM python:3.12-slim
WORKDIR /app
COPY ledgerlens ./ledgerlens
COPY samples ./samples
ENV PORT=8765 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "ledgerlens", "up", "--no-browser"]
