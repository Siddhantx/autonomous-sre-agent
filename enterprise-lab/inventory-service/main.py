import os
import time
import logging
from fastapi import FastAPI, HTTPException, status
import psycopg2
from psycopg2.extras import RealDictCursor

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
logger = logging.getLogger("inventory-service")

app = FastAPI(title="APOE Inventory Service", version="1.0")
FastAPIInstrumentor.instrument_app(app)

DB_CONN_STR = os.getenv("DB_CONN", "postgres://apoe_user:apoe_secure_pass@postgres:5432/enterprise_db")
db_conn = None

def init_db():
    global db_conn
    retry_count = 5
    while retry_count > 0:
        try:
            logger.info("Inventory: Connecting to Postgres...")
            db_conn = psycopg2.connect(DB_CONN_STR)
            db_conn.autocommit = True
            logger.info("Inventory: Postgres connected.")
            break
        except Exception as e:
            logger.warning(f"Inventory DB connection failed: {e}. Retrying in 3s...")
            time.sleep(3)
            retry_count -= 1

    # Create inventory data if not exists
    if db_conn:
        try:
            with db_conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS inventory (
                        item_id VARCHAR(50) PRIMARY KEY,
                        stock INTEGER DEFAULT 100,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Seed mock assets if table is empty
                cur.execute("SELECT COUNT(*) FROM inventory")
                if cur.fetchone()[0] == 0:
                    cur.execute("""
                        INSERT INTO inventory (item_id, stock) VALUES 
                        ('item-A', 500),
                        ('item-B', 150),
                        ('item-C', 0);
                    """)
                    logger.info("Inventory standard items seeded successfully.")
        except Exception as e:
            logger.error(f"Inventory initialization failed: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()

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
        "postgres": "connected" if postgres_ok else "disconnected"
    }

@app.get("/api/v1/inventory/{item_id}")
def get_inventory(item_id: str):
    if not db_conn:
         raise HTTPException(status_code=500, detail="Database unavailable.")
    try:
        with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM inventory WHERE item_id = %s", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Item not found.")
            
            item = dict(row)
            return {
                "item_id": item["item_id"],
                "stock": item["stock"],
                "in_stock": item["stock"] > 0
            }
    except psycopg2.Error as e:
        logger.error(f"Inventory read failure: {e}")
        raise HTTPException(status_code=500, detail="System processing exception on database read.")

@app.post("/api/v1/inventory/restock")
def restock_item(item_id: str, quantity: int):
    if not db_conn:
         raise HTTPException(status_code=500, detail="Database unavailable.")
    try:
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO inventory (item_id, stock) 
                VALUES (%s, %s)
                ON CONFLICT (item_id) 
                DO UPDATE SET stock = inventory.stock + EXCLUDED.stock, updated_at = CURRENT_TIMESTAMP
            """, (item_id, quantity))
        return {"status": "success", "item_id": item_id, "restocked": quantity}
    except Exception as e:
        logger.error(f"Restock execution fail: {e}")
        raise HTTPException(status_code=500, detail="Relational database write error on restock.")
