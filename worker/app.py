from flask import Flask, request, jsonify
import os
import time
from transformers import pipeline

app = Flask(__name__)

# Load the model once at startup, not per request
MODEL = "distilbert-base-uncased-finetuned-sst-2-english"
classifier = pipeline("sentiment-analysis", model=MODEL)

# Optional, test-only delay already configured in docker-compose.yml.
PROCESSING_DELAY = max(0.0, float(os.getenv("PROCESSING_DELAY", "0")))


@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(silent=True)
    if not data or "text" not in data:
        return jsonify(error="JSON body with a 'text' field is required"), 400

    # Delay makes it possible to observe the real AWS request queue.
    if PROCESSING_DELAY:
        time.sleep(PROCESSING_DELAY)

    result = classifier(data["text"])[0]
    return jsonify(
        text=data["text"],
        label=result["label"],
        score=round(result["score"], 4),
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok")
