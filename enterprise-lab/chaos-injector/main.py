import os
import time
import logging
import threading
from fastapi import FastAPI, BackgroundTasks, HTTPException
import psycopg2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chaos-injector")

app = FastAPI(title="APOE Chaos Sandbox Injector", version="1.0")

DB_CONN_STR = os.getenv("DB_CONN", "postgres://apoe_user:apoe_secure_pass@postgres:5432/enterprise_db")
REDIS_CONN_STR = os.getenv("REDIS_CONN", "redis://redis:6379/0")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:29092")
BAD_CONFIG_KEY = "chaos:bad-config:order-service"

# Global variables to sustain leaks/CPU utilization safely
leaked_memory_buffer = []
is_cpu_spike_active = False
pool_hog_connections = []  # held open by the pool-exhaustion scenario


def _ensure_baseline():
    """Create the hot-table index the slow-query scenario drops. Idempotent."""
    try:
        conn = psycopg2.connect(DB_CONN_STR)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)"
            )
        conn.close()
    except Exception as e:
        logger.warning(f"Baseline setup skipped: {e}")


@app.on_event("startup")
def startup():
    _ensure_baseline()

@app.get("/health")
def health():
    return {"status": "chaos-injector-healthy"}

# ==========================================
# CHAOS INJECTION METHODOLOGIES
# ==========================================

# 1. Database Lock Scenario
@app.post("/chaos/db-lock")
def inject_db_lock(background_tasks: BackgroundTasks):
    logger.info("Executing Chaos Injection: Database Deadlock Transaction Scenario...")
    
    def lock_tables():
         try:
             conn1 = psycopg2.connect(DB_CONN_STR)
             conn2 = psycopg2.connect(DB_CONN_STR)
             conn1.autocommit = False
             conn2.autocommit = False
             
             cur1 = conn1.cursor()
             cur2 = conn2.cursor()
             
             # Establish exclusive locking models on order-database tables
             logger.info("Locking schema 'orders' for connection block 1...")
             cur1.execute("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE;")
             
             # Sleep connection to keep transaction holding lock
             logger.info("Suspending transaction scope for 60 seconds (simulated lock)...")
             time.sleep(60)
             
             conn1.rollback()
             conn2.rollback()
             conn1.close()
             conn2.close()
             logger.info("Database transaction locks released.")
         except Exception as e:
             logger.error(f"Error executing raw SQL database locks: {e}")

    background_tasks.add_task(lock_tables)
    return {"status": "triggered", "scenario": "postgres-table-deadlock", "duration": "60s"}

# 2. Infinite CPU Spike Scenario
def cpu_burner_thread():
    global is_cpu_spike_active
    logger.info("Starting math processing loop to spike CPU utilization...")
    start_time = time.time()
    
    # Run a tight matrix simulation loop for 90 seconds
    while is_cpu_spike_active and (time.time() - start_time < 90):
        _ = 12345 * 67890
        
    is_cpu_spike_active = False
    logger.info("CPU spikes target completed. Restoring regular thread scheduler levels.")

@app.post("/chaos/high-cpu")
def inject_high_cpu():
    global is_cpu_spike_active
    if is_cpu_spike_active:
         return {"error": "CPU spike scenario is already active on this sandbox environment."}
    
    is_cpu_spike_active = True
    t = threading.Thread(target=cpu_burner_thread)
    t.start()
    return {"status": "triggered", "scenario": "high-cpu-load", "max_duration": "90s"}

# 3. Dynamic Memory Leak Scenario
def memory_leak_allocator():
    global leaked_memory_buffer
    logger.info("Allocating expanding arrays to simulate Heap Leak (OOM Risk)...")
    
    # Incrementally allocations 
    for i in range(10):
        # Insert heavy structures (roughly 30MB per step)
        leaked_memory_buffer.append(["A" * 1024 * 1024] * 30)
        logger.info(f"Leaked allocation block {i+1}/10 generated successfully.")
        time.sleep(4)

@app.post("/chaos/leak")
def inject_memory_leak(background_tasks: BackgroundTasks):
    background_tasks.add_task(memory_leak_allocator)
    return {"status": "triggered", "scenario": "heap-memory-leak-leakinfo", "allocation_target": "~300MB"}

# ==========================================
# NOVEL FAULTS (no deterministic rule covers these — eval harness targets)
# ==========================================

# 4. Connection-pool exhaustion: hold nearly every Postgres slot open.
@app.post("/chaos/pool-exhaustion")
def inject_pool_exhaustion(background_tasks: BackgroundTasks):
    global pool_hog_connections
    if pool_hog_connections:
        return {"error": "pool-exhaustion already active"}

    probe = psycopg2.connect(DB_CONN_STR)
    with probe.cursor() as cur:
        cur.execute("SHOW max_connections")
        max_conn = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM pg_stat_activity")
        in_use = int(cur.fetchone()[0])
    probe.close()

    target = max(0, max_conn - in_use - 3)  # leave 3 slots so pg stays reachable
    logger.info(f"Opening {target} hog connections ({in_use}/{max_conn} in use)...")
    for _ in range(target):
        try:
            pool_hog_connections.append(psycopg2.connect(DB_CONN_STR))
        except Exception as e:
            logger.warning(f"Stopped early: {e}")
            break

    def auto_release():
        time.sleep(90)
        _release_pool_hogs()

    background_tasks.add_task(auto_release)
    return {"status": "triggered", "scenario": "pool-exhaustion",
            "held_connections": len(pool_hog_connections), "max_duration": "90s"}


def _release_pool_hogs():
    global pool_hog_connections
    for c in pool_hog_connections:
        try:
            c.close()
        except Exception:
            pass
    released = len(pool_hog_connections)
    pool_hog_connections = []
    if released:
        logger.info(f"Released {released} hog connections.")


# 5. Bad config deploy: flip the redis override the order-service honours.
@app.post("/chaos/bad-config")
def inject_bad_config():
    import redis as redis_lib
    r = redis_lib.from_url(REDIS_CONN_STR)
    r.set(BAD_CONFIG_KEY, "DB_CONN=postgres://wrong_user:wrong_pass@nowhere:5432/enterprise_db")
    logger.info("Injected bad config override for order-service (500s until reset).")
    return {"status": "triggered", "scenario": "bad-config-deploy", "key": BAD_CONFIG_KEY}


# 6. Slow-query regression: drop the index on the hot table.
@app.post("/chaos/slow-query")
def inject_slow_query():
    conn = psycopg2.connect(DB_CONN_STR)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_orders_status")
    conn.close()
    logger.info("Dropped idx_orders_status — status queries now seq-scan.")
    return {"status": "triggered", "scenario": "slow-query-regression",
            "dropped_index": "idx_orders_status"}


# 7. Poison-pill Kafka message: malformed bytes on order-events.
@app.post("/chaos/poison-pill")
def inject_poison_pill():
    from kafka import KafkaProducer
    producer = KafkaProducer(bootstrap_servers=KAFKA_BROKER)
    producer.send("order-events", value=b"\x00\xffPOISON-NOT-JSON{{{")
    producer.flush()
    producer.close()
    logger.info("Produced malformed message to order-events.")
    return {"status": "triggered", "scenario": "kafka-poison-pill", "topic": "order-events"}


# 8. Disk-fill on the postgres volume: bulk rows into an unlogged scratch table.
@app.post("/chaos/disk-fill")
def inject_disk_fill(background_tasks: BackgroundTasks):
    def fill():
        try:
            conn = psycopg2.connect(DB_CONN_STR)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE UNLOGGED TABLE IF NOT EXISTS chaos_disk_fill (payload text)"
                )
                for i in range(10):  # ~10 x 20MB batches
                    cur.execute(
                        "INSERT INTO chaos_disk_fill "
                        "SELECT repeat('x', 1024) FROM generate_series(1, 20000)"
                    )
                    logger.info(f"Disk-fill batch {i+1}/10 written.")
            conn.close()
        except Exception as e:
            logger.error(f"Disk-fill error: {e}")

    background_tasks.add_task(fill)
    return {"status": "triggered", "scenario": "disk-fill", "target": "~200MB"}


@app.post("/chaos/reset")
def reset_sandbox():
    global leaked_memory_buffer, is_cpu_spike_active
    leaked_memory_buffer.clear()
    is_cpu_spike_active = False
    _release_pool_hogs()
    try:
        import redis as redis_lib
        redis_lib.from_url(REDIS_CONN_STR).delete(BAD_CONFIG_KEY)
    except Exception as e:
        logger.warning(f"Reset: redis cleanup skipped: {e}")
    try:
        conn = psycopg2.connect(DB_CONN_STR)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS chaos_disk_fill")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)")
        conn.close()
    except Exception as e:
        logger.warning(f"Reset: postgres cleanup skipped: {e}")
    logger.info("Sandbox reset: heap, cpu, pool hogs, bad-config, disk-fill, index restored.")
    return {"status": "cleared", "scenario": "all-purged"}
