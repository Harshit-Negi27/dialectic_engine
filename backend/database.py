import sqlite3
import hashlib
import json
import time
from pathlib import Path

# Define DB path inside the backend folder
DB_PATH = Path(__file__).parent / "cache.db"
SEVEN_DAYS_IN_SECONDS = 7 * 24 * 60 * 60 

def init_db():
    """Creates the table and cleans up old cache."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
   
    c.execute('''CREATE TABLE IF NOT EXISTS analysis_cache 
                 (text_hash TEXT PRIMARY KEY, json_response TEXT, created_at REAL)''')
    
    # Auto-cleanup Logic
    
    cutoff_time = time.time() - SEVEN_DAYS_IN_SECONDS
    
    # Delete anything older than that cutoff
    c.execute("DELETE FROM analysis_cache WHERE created_at < ?", (cutoff_time,))
    deleted_count = c.rowcount
    
    conn.commit()
    conn.close()
    
    if deleted_count > 0:
        print(f"CLEANUP: Removed {deleted_count} expired records from cache.")

def get_cached_analysis(text: str, version: str = ""):
    """Checks for a valid, non-expired cache hit using text and version."""
    raw_key = f"{version}:{text}" if version else text
    text_hash = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT json_response FROM analysis_cache WHERE text_hash=?", (text_hash,))
    row = c.fetchone()
    conn.close()
    
    if row:
        print(f" CACHE HIT! Returning saved result for hash: {text_hash[:8]}... (version: {version or 'none'})")
        return json.loads(row[0])
    return None

def save_analysis_to_cache(text: str, data: dict, version: str = ""):
    """Saves the result with current timestamp using text and version."""
    raw_key = f"{version}:{text}" if version else text
    text_hash = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    current_time = time.time() 
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Insert the hash, the data, AND the current time
    c.execute("INSERT OR REPLACE INTO analysis_cache VALUES (?, ?, ?)", 
              (text_hash, json.dumps(data), current_time))
    conn.commit()
    conn.close()
    print(f" CACHE SAVED! Hash: {text_hash[:8]}...")