# workers=1 is INTENTIONAL — ThreadPoolExecutor singleton must be process-wide.
# If you need workers > 1, switch to Redis Queue (see archive/README.md).
web: gunicorn server:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT --worker-class gthread
