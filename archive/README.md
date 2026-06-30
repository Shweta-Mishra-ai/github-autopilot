# archive/

These files are NOT active. Kept for reference and future use.

| File | Why archived |
|------|-------------|
| tasks.py | Celery task system — written but never wired. ThreadPool is used instead. |
| celery_app.py | Celery config — goes with tasks.py |
| queue_consumer.py | Redis Streams consumer — never called from server.py |
| queue_producer.py | Redis Streams producer — never called from server.py |
| handlers_v1.py | V1 monolith (506 lines) — replaced by app/handlers/ package |
| storage_events.py | SQLite event log — never wired in production |
| storage_fixtures.py | Webhook fixture capture — never wired in production |

## When to activate Redis Queue

Switch from ThreadPool to Redis Queue (tasks.py + queue_producer/consumer) when:
- Events dropped per day > 5 consistently
- Pool saturation average > 60%
- Webhook processing time > 30s average
- Concurrent repos > 10
- You need guaranteed retry-on-failure (not just HTTP 503)

Wire steps:
1. queue_producer.py: call from server.py instead of thread_pool.dispatch()
2. queue_consumer.py: run as separate Render Background Worker service
3. tasks.py: contains the handler dispatch logic
4. celery_app.py: OR use Redis Streams directly (lighter than Celery)
