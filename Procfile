# Procfile — AI Repo Manager V4
#
# PHASE 1 — Free Tier ($0/month)
# Both web and worker run as separate Render services (both on free plan).
# Celery Beat runs inside the worker process (--beat flag).
# No persistent disk needed — Qdrant Cloud handles vector storage.
#
web: gunicorn server:app --workers 1 --timeout 60 --bind 0.0.0.0:$PORT
worker: celery -A app.celery_app worker --beat -c 2 -Q high,medium,low --loglevel=warning

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — When first real users arrive ($7/month for worker upgrade)
# Uncomment below and delete Phase 1 lines above.
#
# web: gunicorn server:app --workers 2 --timeout 30 --bind 0.0.0.0:$PORT
# worker: celery -A app.celery_app worker --beat -c 4 -Q high,medium,low --loglevel=info
#
# PHASE 3 — Growing (50+ active repos) — Split into 3 services
# web:    gunicorn server:app --workers 2 --timeout 30 --bind 0.0.0.0:$PORT
# worker_high: celery -A app.celery_app worker -Q high -c 3 --loglevel=info
# worker_low:  celery -A app.celery_app worker --beat -Q medium,low -c 2 --loglevel=info
