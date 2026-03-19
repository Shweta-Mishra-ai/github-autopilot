# ✅ CHANGED — workers 1 → 2, gthread worker class add kiya, timeout 120 → 30
# max-requests 1000 — memory leak se bachne ke liye worker auto-restart hoga
# Pehle: gunicorn server:app --workers 1 --timeout 120
web: gunicorn server:app --workers 2 --worker-class gthread --threads 4 --timeout 30 --max-requests 1000 --max-requests-jitter 100
worker: python worker.py
