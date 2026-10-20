FROM python:3.11-slim
WORKDIR /app

RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download the model at build time so container startup is fast
RUN python -c "from transformers import pipeline; pipeline('sentiment-analysis', model='distilbert-base-uncased-finetuned-sst-2-english')"

COPY app.py .
EXPOSE 5000
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--timeout", "120", "app:app"]