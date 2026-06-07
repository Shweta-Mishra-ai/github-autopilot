# archive/

Yeh files active nahi hain. Future reference ke liye rakhi hain.

| File | Kyun archived |
|------|--------------|
| tasks.py | Celery task system — likha tha lekin wire nahi kiya. ThreadPool use ho raha hai. |
| celery_app.py | Celery config — tasks.py ke saath jaata hai |
| queue_consumer.py | Redis Streams consumer — server.py se kabhi call nahi hua |
| queue_producer.py | Redis Streams producer — server.py se kabhi call nahi hua |
| worker.py | Standalone worker — use nahi hota |
| handlers_v1.py | V1 monolith (506 lines) — app/handlers/ package ne replace kiya |
| storage_events.py | SQLite event log — production mein wire nahi hua |
| storage_fixtures.py | Webhook fixture capture — production mein wire nahi hua |

## Future use
- Celery wiring: tasks.py + celery_app.py ko server.py mein wire karo
  jab user load badhe aur ThreadPool kafi na rahe
