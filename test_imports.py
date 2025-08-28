try:
    import telethon, PIL, PyPDF2, pydub, moviepy.editor, psycopg, flask, aiohttp
    print("✅ All imports successful!")
except Exception as e:
    print(f"❌ Import error: {e}")