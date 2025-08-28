import os
import psycopg

def _conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    if "sslmode=" not in url:
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return psycopg.connect(url, autocommit=True)

def init_db():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS filebot_users (
                user_id BIGINT PRIMARY KEY
            )
            """)

def add_user(user_id: int):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO filebot_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,)
            )

def get_all_users():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM filebot_users")
            rows = cur.fetchall()
            return [r[0] for r in rows]