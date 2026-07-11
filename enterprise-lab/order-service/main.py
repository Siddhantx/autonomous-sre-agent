import os
import json
import time
import logging
from fastapi import FastAPI, HTTPException, status
import redis
import psycopg2
from psycopg2.extras import RealDictCursor
from kafka import KafkaProducer
import httpx

# ==========================================
# OPENTELEMETRY INITIALIZATION
# ==========================================
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Configure global tracer
provider = TracerProvider()
otel_collector_url = os.getenv("OPENTELEMETRY_COLLECTOR_URL", "http://otel-collector:4317")
processor = SimpleSpanProcessor(OTLPSpanExporter(endpoint=otel_collector_url, insecure=True))
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("order-service")

app = FastAPI(title="APOE Order Service", version="1.0")
FastAPIInstrumentor.instrument_app(app)

# ==========================================
# DEPENDENCY CONNECT RULES WITH RETRIES
# ==========================================
DB_CONN_STR = os.getenv("DB_CONN", "postgres://apoe_user:apoe_secure_pass@postgres:5432/enterprise_db")
REDIS_CONN_STR = os.getenv("REDIS_CONN", "redis://redis:6379/0")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:29092")

db_conn = None
redis_client = None
kafka_producer = None

def init_connections():
    global db_conn, redis_client, kafka_producer
    
    # 1. Connect Postgres
    retry_count = 5
    while retry_count > 0:
        try:
            logger.info("Connecting to Postgres...")
            db_conn = psycopg2.connect(DB_CONN_STR)
            db_conn.autocommit = True
            logger.info("Postgres connected successfully!")
            break
        except Exception as e:
            logger.warning(f"Postgres connection failed: {e}. Retrying in 3s...")
            time.sleep(3)
            retry_count -= 1
            
    # Initialize DB schema if needed
    if db_conn:
        try:
            with db_conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        id VARCHAR(50) PRIMARY KEY,
                        item_id VARCHAR(50),
                        quantity INTEGER,
                        amount NUMERIC(10, 2),
                        status VARCHAR(20),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                logger.info("Database schema initialized/checked.")
        except Exception as e:
            logger.error(f"Failed to run schema scripts: {e}")

    # 2. Connect Redis
    try:
        logger.info("Connecting to Redis...")
        redis_client = redis.from_url(REDIS_CONN_STR)
        redis_client.ping()
        logger.info("Redis connected successfully!")
    except Exception as e:
        logger.error(f"Redis connection failed: {e}")

    # 3. Connect Kafka Producer
    retry_count = 5
    while retry_count > 0:
        try:
            logger.info("Connecting to Kafka...")
            kafka_producer = KafkaProducer(
                bootstrap_servers=KAFKA_BROKER,
                value_serializer=lambda v: json.dumps(v).encode('utf-8')
            )
            logger.info("Kafka Producer initialized.")
            break
        except Exception as e:
            logger.warning(f"Kafka connection failed: {e}. Retrying in 5s...")
            time.sleep(5)
            retry_count -= 1

@app.on_event("startup")
async def startup_event():
    init_connections()

# ==========================================
# REST API ENDPOINTS
# ==========================================
@app.get("/health")
def health_endpoint():
    try:
        if redis_client:
            redis_client.ping()
        postgres_ok = False
        if db_conn:
            with db_conn.cursor() as cur:
                cur.execute("SELECT 1")
                postgres_ok = True
        return {
            "status": "healthy",
            "postgres": "connected" if postgres_ok else "disconnected",
            "redis": "connected" if redis_client else "disconnected",
            "kafka": "connected" if kafka_producer else "disconnected"
        }
    except Exception as e:
        logger.error(f"Health check exception: {e}")
        raise HTTPException(status_code=500, detail=f"Unhealthy: {str(e)}")

@app.post("/api/v1/orders", status_code=status.HTTP_201_CREATED)
async def create_order(item_id: str, quantity: int, amount: float):
    # Retrieve dependency details from inventory-service
    inventory_url = f"http://inventory-service:8082/api/v1/inventory/{item_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(inventory_url, timeout=5.0)
            if resp.status_code != 200:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to retrieve inventory verification.")
            inv_data = resp.json()
            if not inv_data.get("in_stock", False) or inv_data.get("stock", 0) < quantity:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient stock item level.")
    except httpx.RequestError as e:
        logger.error(f"Upstream inventory dependency API timeout/failure: {e}")
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Inventory service communication timeout.")

    order_id = f"ord-{int(time.time()*1000)}"

    # Write to relational store
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (id, item_id, quantity, amount, status) VALUES (%s, %s, %s, %s, %s)",
                (order_id, item_id, quantity, amount, "PENDING")
            )
    except Exception as e:
        logger.error(f"Postgres insert failure: {e}")
        raise HTTPException(status_code=500, detail="Database write error.")

    # Write Cache hit layer
    if redis_client:
        try:
            redis_client.setex(
                f"order:{order_id}",
                3600,
                json.dumps({"id": order_id, "item_id": item_id, "quantity": quantity, "amount": amount, "status": "PENDING"})
            )
        except Exception as e:
            logger.warning(f"Failed to populate Redis cache: {e}")

    # Emit transition event to Kafka topic 'order-events'
    if kafka_producer:
        try:
            event = {
                "order_id": order_id,
                "item_id": item_id,
                "quantity": quantity,
                "amount": amount,
                "status": "PENDING"
            }
            kafka_producer.send("order-events", key=order_id.encode('utf-8'), value=event)
            kafka_producer.flush()
        except Exception as e:
            logger.warning(f"Failed to publish transaction payload to Kafka: {e}")

    return {"order_id": order_id, "status": "PENDING"}

@app.get("/api/v1/orders/{order_id}")
def get_order(order_id: str):
    # Attempt Redis cache read first (Fast path)
    if redis_client:
        try:
            cache = redis_client.get(f"order:{order_id}")
            if cache:
                return json.loads(cache)
        except Exception as e:
            logger.warning(f"Cache lookup failure: {e}")

    # Relational Database lookup fallback (Slow path)
    if not db_conn:
        raise HTTPException(status_code=500, detail="Database unavailable.")
    
    try:
        with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Order not found.")
            # Repopulate Cache
            if redis_client:
                # convert Decimals to strings for JSON safety
                clean_row = dict(row)
                clean_row['amount'] = float(clean_row['amount']) if clean_row['amount'] else 0.0
                clean_row['created_at'] = str(clean_row['created_at'])
                redis_client.setex(f"order:{order_id}", 3600, json.dumps(clean_row))
                return clean_row
            return dict(row)
    except psycopg2.Error as e:
        logger.error(f"Database query failed: {e}")
        raise HTTPException(status_code=500, detail="Relational lookup failed.")
