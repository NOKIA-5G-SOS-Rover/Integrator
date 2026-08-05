FROM python:3.11-slim

WORKDIR /app

COPY python/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY python/ ./python/

ENV BACKEND_URL=http://localhost:5000
ENV ROVER_ID=ROVER-Q1
ENV CAMERA_ID=CAM-01

EXPOSE 8082

CMD ["python", "python/person_detector_server.py", "--host", "0.0.0.0", "--port", "8082"]
