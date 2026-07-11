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

# Global variables to sustain leaks/CPU utilization safely
leaked_memory_buffer = []
is_cpu_spike_active = False

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

@app.post("/chaos/reset")
def reset_sandbox():
    global leaked_memory_buffer, is_cpu_spike_active
    leaked_memory_buffer.clear()
    is_cpu_spike_active = False
    logger.info("Resetting local sandbox telemetry metrics. Purging heap leakage memory.")
    return {"status": "cleared", "scenario": "all-purged"}
