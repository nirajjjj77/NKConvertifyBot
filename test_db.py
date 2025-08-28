from dotenv import load_dotenv
load_dotenv()
from db import init_db
try:
    init_db()
    print("✅ Database connection successful!")
except Exception as e:
    print(f"❌ Database error: {e}")