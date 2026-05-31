# Procfile — AI Repo Manager V4
# Single service — web only. Threading handles all async processing.
# No separate Celery worker needed on free tier.
web: gunicorn server:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT --worker-class sync
