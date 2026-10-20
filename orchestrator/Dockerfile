FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py status.json ./
EXPOSE 80
CMD ["gunicorn", "-b", "0.0.0.0:80", "-w", "1", "--threads", "32", "--timeout", "300", "app:app"]