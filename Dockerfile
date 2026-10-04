FROM python:3.13-slim
WORKDIR /app
COPY vendor/ vendor/
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r backend/requirements.txt
COPY backend/ backend/
WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "serve.py"]
