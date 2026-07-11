import os
import json
import time
import logging
import threading
from fastapi import FastAPI, HTTPException
import psycopg2
import redis
from kafka import KafkaConsumer

# ==========================================
# OPENTELEMETRY INITIALIZATION
# ==========================================
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

provider = TracerProvider()
otel_collector_url = os.getenv("OPENTELEMETRY_COLLECTOR_URL", "http://otel-collector:4317")
processor = SimpleSpanProcessor(OTLPSpanExporter(endpoint=otel_collector_url, insecure=True))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payment-service")

app = FastAPI(title="APOE Payment Service", version="1.0")
FastAPIInstrumentor.instrument_app(app)

DB_CONN_STR = os.getenv("DB_CONN", "postgres://apoe_user:apoe_secure_pass@postgres:5432/enterprise_db")
REDIS_CONN_STR = os.getenv("REDIS_CONN", "redis://redis:6379/0")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:29092")

db_conn = None
redis_client = None

def init_connections():
    global db_conn, redis_client
    # 1. Connect Postgres
    retry = 5
    while retry > 0:
        try:
            logger.info("Payment Service: Connecting to Postgres...")
            db_conn = psycopg2.connect(DB_CONN_STR)
            db_conn.autocommit = True
            logger.info("Payment Service: Postgres connected successfully.")
            break
        except Exception as e:
            logger.warning(f"Payment DB connection failed: {e}. Retrying...")
            time.sleep(3)
            retry -= 1

    # 2. Connect Redis
    try:
        redis_client = redis.from_url(REDIS_CONN_STR)
        redis_client.ping()
        logger.info("Payment Service: Redis connected successfully.")
    except Exception as e:
        logger.error(f"Payment Redis connection failed: {e}")

# ==========================================================
# KAFKA TRANSACTION CONSUMER LOOP (Asynchronous Worker)
# ==========================================================
def consume_order_events():
    init_connections()
    logger.info("Starting background consumer for topic 'order-events'...")
    
    consumer = None
    retry = 10
    while retry > 0:
        try:
            consumer = KafkaConsumer(
                "order-events",
                bootstrap_servers=KAFKA_BROKER,
                group_id="payment-processors",
                value_serializer=None, # Raw bytes
                auto_offset_reset="earliest"
            )
            logger.info("Kafka Consumer bound to 'order-events' matches successfully.")
            break
        except Exception as e:
            logger.warning(f"Kafka Consumer binding failed: {e}. Retrying in 5s...")
            time.sleep(5)
            retry -= 1

    if not consumer:
        logger.error("Kafka consumer initialization failed permanently. Payment service operating strictly on fallback.")
        return

    for msg in consumer:
        try:
            # Decode message
            payload = json.loads(msg.value.decode('utf-8'))
            order_id = payload.get("order_id")
            amount = payload.get("amount", 0.0)
            logger.info(f"Received transaction event for Order ID: {order_id} (Amount: {amount})")

            # Simulate heavy validation/clearing pipeline
            time.sleep(1.5) 
            
            payment_status = "PAID" if amount < 10000.0 else "FAILED" # Reject massive anomalous order counts as fake transactions
            
            # Update Postgres primary ledger
            if db_conn:
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE orders SET status = %s WHERE id = %s",
                        (payment_status, order_id)
                    )
                    logger.info(f"Postgres inventory status updated to {payment_status} for {order_id}.")
                    
            # Update Redis caching layer (sync-match updates)
            if redis_client:
                cache = redis_client.get(f"order:{order_id}")
                if cache:
                    order_obj = json.loads(cache)
                    order_obj["status"] = payment_status
                    redis_client.setex(f"order:{order_id}", 3600, json.dumps(order_obj))
                    logger.info(f"Redis cache synced status details for {order_id} modified.")
        except Exception as e:
            logger.error(f"Failed to process transaction message stream event: {e}")

@app.on_event("startup")
async def startup_event():
    # Run consumer in dedicated concurrent execution thread context
    t = threading.Thread(target=consume_order_events, daemon=True)
    t.start()

@app.get("/health")
def health():
    postgres_ok = False
    if db_conn:
        try:
            with db_conn.cursor() as cur:
                cur.execute("SELECT 1")
                postgres_ok = True
        except:
            pass
    return {
        "status": "healthy",
        "postgres": "connected" if postgres_ok else "disconnected",
        "redis": "connected" if redis_client else "disconnected"
    }
