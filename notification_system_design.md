# Stage 1 - Intern-Level REST API Design (Placement Notification System)

## Objective

Build a student notification service for placement season where users can:

- view notifications
- mark notifications as read
- see unread badge count
- receive live updates in app

## API Base

`/api/v1`

## Main Entities

### Notification

```json
{
  "id": "uuid",
  "studentId": "uuid",
  "category": "PLACEMENT",
  "title": "TCS Round 2 Shortlist",
  "message": "Interview at 3 PM, Seminar Hall",
  "priority": "HIGH",
  "createdAt": "2026-05-06T07:10:00Z",
  "isRead": false
}
```

### Allowed values

- `category`: `PLACEMENT | EVENT | RESULT | GENERAL`
- `priority`: `LOW | MEDIUM | HIGH | CRITICAL`

## Stage 1 Endpoints

### 1) Get all notifications of logged-in student

**GET** `/notifications`

Query params:

- `category` (optional)
- `isRead` (optional)
- `limit` (optional, default `20`)
- `cursor` (optional)

Success response:

```json
{
  "data": [
    {
      "id": "uuid",
      "studentId": "uuid",
      "category": "PLACEMENT",
      "title": "TCS Round 2 Shortlist",
      "message": "Interview at 3 PM, Seminar Hall",
      "priority": "HIGH",
      "createdAt": "2026-05-06T07:10:00Z",
      "isRead": false
    }
  ],
  "page": {
    "nextCursor": "base64_cursor",
    "hasMore": true
  }
}
```

### 2) Get single notification by id

**GET** `/notifications/{notificationId}`

### 3) Mark one notification as read

**PATCH** `/notifications/{notificationId}/read`

Request body:

```json
{
  "isRead": true
}
```

### 4) Mark all notifications as read

**PATCH** `/notifications/read-all`

Optional body:

```json
{
  "category": "PLACEMENT"
}
```

### 5) Get unread count for badge

**GET** `/notifications/unread-count`

Sample response:

```json
{
  "data": {
    "totalUnread": 6
  }
}
```

### 6) Real-time updates (SSE)

**GET** `/notifications/stream`

SSE event sample:

```text
event: notification.created
id: evt_1001
data: {"id":"uuid","title":"Infosys test link live","category":"PLACEMENT"}
```

## HTTP Status Codes Used

- `200` success
- `400` bad request
- `401` unauthorized
- `404` not found
- `422` validation error
- `500` internal server error

## Stage 1 Outcome

At this stage, frontend and backend integration for core notification flow is ready.

# Stage 2 - Intern-Level Database Design and SQL Mapping

## Objective

Store notifications properly and support all Stage 1 APIs using SQL queries.

## Database Choice

Use **PostgreSQL** because:

- simple and reliable for college project + placement exam
- strong support for joins and indexes
- easy to write read/unread queries

## Tables

```sql
CREATE TABLE notifications (
    id UUID PRIMARY KEY,
    category VARCHAR(20) NOT NULL,
    title VARCHAR(200) NOT NULL,
    message TEXT NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'MEDIUM',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE student_notifications (
    student_id UUID NOT NULL,
    notification_id UUID NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ NULL,
    PRIMARY KEY (student_id, notification_id)
);
```

## Indexes

```sql
CREATE INDEX idx_student_notifications_student_read
ON student_notifications (student_id, is_read);

CREATE INDEX idx_notifications_created_at
ON notifications (created_at DESC);
```

## API to SQL Mapping

### GET `/notifications`

```sql
SELECT n.id, sn.student_id, n.category, n.title, n.message, n.priority, n.created_at, sn.is_read
FROM student_notifications sn
JOIN notifications n ON n.id = sn.notification_id
WHERE sn.student_id = :student_id
  AND (:category IS NULL OR n.category = :category)
  AND (:is_read IS NULL OR sn.is_read = :is_read)
ORDER BY n.created_at DESC
LIMIT :limit;
```

### PATCH `/notifications/{id}/read`

```sql
UPDATE student_notifications
SET is_read = TRUE,
    read_at = NOW()
WHERE student_id = :student_id
  AND notification_id = :notification_id;
```

### GET `/notifications/unread-count`

```sql
SELECT COUNT(*) AS total_unread
FROM student_notifications
WHERE student_id = :student_id
  AND is_read = FALSE;
```

## Stage 2 Risks + Fix

- **Risk:** query gets slow when rows grow.
- **Fix:** add indexes on `(student_id, is_read)` and sort column.
- **Risk:** unread count endpoint hit very frequently.
- **Fix:** cache unread count in Redis for short time (optional extension).

## Stage 2 Outcome

Now APIs from Stage 1 are fully backed by SQL schema and queries.

# Stage 3 - Query Correction and Performance Improvement

Given query:

```sql
SELECT * FROM notifications
WHERE studentID = 1042 AND isRead = false
ORDER BY createdAt DESC;
```

## 1) Is this query correct?

Not fully correct for scalable production usage.

- `SELECT *` pulls all columns (unnecessary data).
- no `LIMIT`, so it may return too many rows.
- no pagination, so next page handling is weak.

## 2) Better query

```sql
SELECT id, studentID, notificationType, message, isRead, createdAt
FROM notifications
WHERE studentID = 1042
  AND isRead = false
ORDER BY createdAt DESC, id DESC
LIMIT 50;
```

## 3) Pagination query (next page)

```sql
SELECT id, studentID, notificationType, message, isRead, createdAt
FROM notifications
WHERE studentID = 1042
  AND isRead = false
  AND (createdAt, id) < (:last_created_at, :last_id)
ORDER BY createdAt DESC, id DESC
LIMIT 50;
```

## 4) Required index

```sql
CREATE INDEX idx_notifications_student_read_created
ON notifications (studentID, isRead, createdAt DESC, id DESC);
```

## 5) Why this is faster

- DB directly jumps to student unread rows.
- Sort is already aligned with index.
- Less disk scan and lower response time.

## 6) Extra asked query

Students who got placement notifications in last 7 days:

```sql
SELECT DISTINCT studentID
FROM notifications
WHERE notificationType = 'PLACEMENT'
  AND createdAt >= NOW() - INTERVAL '7 days';
```

## Stage 3 Outcome

Query is now optimized and ready for large data growth.

# Stage 4 - Handling Heavy Page-Load Traffic and DB Overload

## Problem Statement

Right now notifications are fetched from DB on every page load for every student.
This causes:

- very high DB reads
- slower API response
- poor user experience during peak time

## Goal in Stage 4

Reduce DB load and still keep notifications fresh for users.

## Strategy 1: Add Redis Cache for Notification List + Unread Count

### Idea

- Store recent notification list in Redis per student.
- Store unread count in Redis.
- Serve from cache first, then DB only on cache miss.

### Example cache keys

- `notif:list:{studentId}:{cursor}`
- `notif:unread:{studentId}`

### Tradeoffs

- **Pros:** very fast reads, major DB load reduction.
- **Cons:** cache invalidation logic required after new notification/read action.

## Strategy 2: Do Incremental Fetch Instead of Full Fetch

### Idea

- On first load fetch latest 20.
- On next loads fetch only notifications after last seen timestamp/cursor.

### Tradeoffs

- **Pros:** much smaller payload and fewer DB rows scanned.
- **Cons:** frontend must maintain `lastSeenCursor` correctly.

## Strategy 3: Keep Persistent SSE Connection (Avoid Re-fetch on Every Page Open)

### Idea

- Load notification data once.
- Receive new notifications through SSE stream (`/notifications/stream`).
- Update UI directly when event arrives.

### Tradeoffs

- **Pros:** near real-time UX and fewer repetitive read queries.
- **Cons:** handling reconnect and missed events adds implementation complexity.

## Strategy 4: Precompute and Store Unread Count

### Idea

- Maintain unread count in a separate table like `student_unread_counter`.
- Update this counter when inserting new notification or marking read.
- API reads count directly (O(1) style lookup).

### Tradeoffs

- **Pros:** unread badge endpoint becomes very fast.
- **Cons:** must ensure counter update stays transactional and accurate.

## Strategy 5: DB-Level Optimizations

### Actions

- Keep composite indexes for common filter/sort columns.
- Use keyset pagination (`createdAt`, `id`) only.
- Partition old notification data by month if data is huge.

### Tradeoffs

- **Pros:** stable performance for large datasets.
- **Cons:** extra DBA maintenance and migration effort.

## Recommended Combined Approach (Best Practical Answer)

Use these together:

1. Redis caching for list + unread count.
2. Incremental/keyset fetching.
3. SSE for live updates.
4. Composite indexes in DB.

This gives best balance of speed, scalability, and implementation effort.

## Simple Request Flow After Improvement

1. User opens app.
2. API checks Redis cache first.
3. If cache hit, return instantly.
4. If miss, query DB with indexed + paginated query and refresh cache.
5. New notifications come via SSE, not by full reload.

## Expected Performance Improvement

- Significant reduction in DB read QPS.
- Faster first meaningful response to user.
- Better experience during placement peak traffic.

## Final Tradeoff Summary

- Caching improves speed but needs invalidation.
- SSE improves UX but needs reconnect handling.
- Precomputed counters improve badge performance but require consistency logic.
- Partitioning helps at very high scale but increases operational complexity.

# Stage 5 - Reliable High-Volume Notify All (50,000 Students)

## Shortcomings in the Given Implementation

Given pseudocode:

```python
function notify_all(student_ids: array, message: string):
    for student_id in student_ids:
        send_email(student_id, message)
        save_to_db(student_id, message)
        push_to_app(student_id, message)
```

Major issues:

- **No failure isolation:** if `send_email` fails for one student, flow becomes inconsistent for that student.
- **No retry mechanism:** transient email provider/network failures are not retried.
- **No idempotency:** rerunning job may duplicate DB rows or duplicate pushes.
- **Slow and non-scalable:** single loop is too slow for 50,000 users and blocks on external API latency.
- **Tight coupling of channels:** email + DB + app push are done synchronously in one path.
- **No observability/control:** no batch/job ID, delivery status, dead-letter handling, or partial-progress recovery.

## What if `send_email` failed for 200 students?

Do **not** re-run the whole blast blindly. Use delivery-state tracking:

- Keep a per-recipient per-channel status (`PENDING | SENT | FAILED | RETRYING`).
- Retry only failed email deliveries with exponential backoff.
- Move permanently failing deliveries to a DLQ (dead-letter queue) after max attempts.
- Keep in-app notification independent so students still see alerts even if email is delayed.

## Should DB save and email send happen together?

They should happen in **one business flow**, but **not as one synchronous remote transaction**.

Recommended pattern:

1. Persist notification + recipients + outbox event in a single DB transaction.
2. Commit transaction first (source of truth is durable).
3. Background workers consume outbox/queue and perform email + app delivery asynchronously.

Why:

- External providers (email API) cannot participate in DB atomic transaction.
- This prevents data loss and avoids "email sent but DB missing" or "DB saved but request crashed before send" inconsistencies.
- System stays fast for HR click action (quick enqueue), while workers scale independently.

## Revised Pseudocode (Reliable + Fast)

```python
# HR API handler
function notify_all(student_ids: array, message: string, title: string):
    request_id = uuid()  # idempotency key for this notify-all action
    now = current_timestamp()

    begin_transaction()
        notification_id = insert_notifications(
            id=uuid(),
            title=title,
            message=message,
            created_at=now,
            request_id=request_id
        )

        
        bulk_insert_notification_recipients(
            notification_id=notification_id,
            student_ids=student_ids,
            email_status="PENDING",
            app_status="PENDING",
            attempt_count=0
        )

        
        insert_outbox_event(
            event_type="NOTIFICATION_CREATED",
            aggregate_id=notification_id,
            payload={
                "notification_id": notification_id,
                "request_id": request_id
            }
        )
    commit_transaction()

    return {
        "notification_id": notification_id,
        "request_id": request_id,
        "accepted_recipients": len(student_ids),
        "status": "QUEUED"
    }



function publish_outbox_events():
    events = fetch_unpublished_outbox_batch(limit=500)
    for event in events:
        queue_publish("notification.dispatch", event.payload, key=event.aggregate_id)
        mark_outbox_published(event.id)



function dispatch_notification_job(job):
    recipients = fetch_recipients_in_batches(job.notification_id, batch_size=1000)

    for batch in recipients:
        parallel_for_each(batch, max_workers=100):
            enqueue_email_task(
                notification_id=job.notification_id,
                student_id=recipient.student_id,
                idempotency_key=job.request_id + ":" + recipient.student_id + ":email"
            )
            enqueue_app_task(
                notification_id=job.notification_id,
                student_id=recipient.student_id,
                idempotency_key=job.request_id + ":" + recipient.student_id + ":app"
            )



function process_email_task(task):
    if already_sent(task.idempotency_key):
        return

    try:
        email_provider_send(task.student_id, task.notification_id)
        mark_email_status(task.notification_id, task.student_id, "SENT")
        store_idempotency_success(task.idempotency_key)
    except TransientError:
        increment_attempt(task.notification_id, task.student_id, channel="email")
        if attempt_count < MAX_RETRIES:
            requeue_with_backoff(task)
            mark_email_status(task.notification_id, task.student_id, "RETRYING")
        else:
            move_to_dlq(task)
            mark_email_status(task.notification_id, task.student_id, "FAILED")
    except PermanentError:
        move_to_dlq(task)
        mark_email_status(task.notification_id, task.student_id, "FAILED")


# In-app delivery worker
function process_app_task(task):
    if already_sent(task.idempotency_key):
        return

    save_student_notification_row(task.notification_id, task.student_id)
    publish_realtime_event(task.student_id, task.notification_id)  # SSE/WebSocket
    mark_app_status(task.notification_id, task.student_id, "SENT")
    store_idempotency_success(task.idempotency_key)
```

## Stage 5 Outcome

This design gives:

- Fast HR response (request accepted quickly, not blocked on 50,000 sends).
- Reliable delivery with retries + DLQ.
- Safe partial failure handling (retry only failed subset, e.g., 200 students).
- Consistent state through transactional persistence + outbox.
- Horizontal scalability with worker concurrency and batching.

# Stage 6 - Priority Inbox (Python Implementation)

## Objective

Build a Python implementation that always keeps top `n` most important unread notifications first, where importance combines:

1. weighted type priority (`Placement > Result > Event`)
2. recency (`Timestamp`)

Code file added: `priority_inbox.py`

## Weight Model Used

- `Placement`: `300`
- `Result`: `200`
- `Event`: `100`
- unknown type: `0`

Ranking key:
`(type_weight, timestamp_epoch)`

So type priority is dominant, and for same type the newer notification comes first.

## How Top 10 Is Maintained Efficiently While New Notifications Keep Arriving

Data structure:

- fixed-size **min-heap** of size `n` (default `10`)
- `seen_ids` set to avoid duplicates

Complexity:

- ingest each notification: `O(log n)`
- memory for ranking: `O(n)` for heap + dedupe set for observed IDs

Flow:

1. Fetch notifications from protected API.
2. Parse and validate each payload.
3. Convert into ranked entries.
4. Keep only best `n` in heap via `heappush` / `heappushpop`.
5. Print sorted snapshot for output/screenshots.

## Run Commands (Python)

One-time run with sample data (no network):

```bash
python priority_inbox.py --demo --once --n 10
```

One-time run with live API:

```bash
python priority_inbox.py --once --n 10
```

Continuous mode (poll every 20s):

```bash
python priority_inbox.py --n 10 --poll-interval 20
```

## Stage 7 Outcome

The Priority Inbox is implemented in Python as a functioning code file that fetches notifications, ranks by weighted priority + recency, and keeps top `n` efficiently as fresh notifications arrive.