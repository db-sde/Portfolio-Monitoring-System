FROM python:3.13-slim

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/

# cwd matches backend/'s own location so config.json etc. resolve where
# expected even without CONFIG_PATH/CAS_DATA_PATH/CACHE_PATH env vars set
# (belt-and-suspenders — the app itself also resolves those relative to
# each module's __file__, not cwd, but this keeps behavior obvious too).
WORKDIR /app/backend

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
