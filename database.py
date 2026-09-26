import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "traffic.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        timestamp TEXT NOT NULL,
        vehicle_count INTEGER,
        avg_speed REAL,
        traffic_density REAL,
        prediction_level INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    conn.commit()
    conn.close()


def create_user(username, password):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        return user_id
    except sqlite3.IntegrityError:
        return None
    except Exception:
        return None


def verify_user(username, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, password FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if row and row["password"] == password:
        return int(row["id"])
    return None


def save_prediction(user_id, vehicle_count, avg_speed, traffic_density, prediction_level):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO predictions (user_id, timestamp, vehicle_count, avg_speed, traffic_density, prediction_level) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, datetime.utcnow().isoformat(), vehicle_count, avg_speed, traffic_density, prediction_level)
    )
    conn.commit()
    conn.close()
