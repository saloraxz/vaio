FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy API and frontend
COPY main.py .
COPY frontend/ frontend/

# data.save is mounted at runtime via volume — not baked in
ENV CSRS_DATA_FILE=/data/data.save
ENV CSRS_FRONTEND_DIR=/app/frontend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
