# workers=1 is INTENTIONAL — ThreadPoolExecutor singleton must be process-wide.
# To scale out, run worker.py as a separate process and set
# EVENT_QUEUE_CONSUMERS=0 here; see the scaling notes in render.yaml.
web: gunicorn server:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT --worker-class gthread
