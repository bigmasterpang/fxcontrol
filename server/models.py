"""
Database Models & Helpers for FX Manager
SQLite database managing users, device bindings, IP rate limits, and system settings.
"""
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime

DB_PATH = "/var/www/fx-manager/fx.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return secrets.compare_digest(key.hex(), expected_hash)

def init_db(db_file=None):
    global DB_PATH
    if db_file:
        DB_PATH = db_file
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 1. Users Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                registered_ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'active'
            )
        ''')
        
        # 2. Devices Table (MAC as unique identifier)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT UNIQUE NOT NULL,
                device_type TEXT NOT NULL,
                custom_name TEXT,
                bound_user_id INTEGER,
                bound_at TIMESTAMP,
                last_seen_ip TEXT,
                last_seen_at TIMESTAMP,
                plug_names TEXT,
                sort_order INTEGER DEFAULT 0,
                FOREIGN KEY (bound_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        ''')
        
        # Migration: ensure plug_names and sort_order columns exist for existing database
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN plug_names TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception:
            pass
        
        # 3. Registration Logs Table for IP frequency & limit tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS registration_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                username TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 4. Settings Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Insert default settings if not exist
        default_settings = {
            "max_users_per_ip": "3",
            "allow_registration": "1",
            "cooldown_seconds": "60"
        }
        for k, v in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
            
        # Create default admin user (admin / admin123) if no admin exists
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            admin_pwd = os.environ.get("ADMIN_DEFAULT_PASSWORD", "admin123")
            pwd_hash, salt = hash_password(admin_pwd)
            cursor.execute('''
                INSERT INTO users (username, password_hash, salt, role, registered_ip, status)
                VALUES (?, ?, ?, 'admin', '127.0.0.1', 'active')
            ''', ("admin", pwd_hash, salt))
            print("[DB] Default admin user initialized (admin / admin123).")
            
        conn.commit()

# --- User Queries ---
def get_user_by_username(username: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        return cursor.fetchone()

def get_user_by_id(user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cursor.fetchone()

def create_user(username: str, password: str, ip: str, role: str = 'user'):
    pwd_hash, salt = hash_password(password)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (username, password_hash, salt, role, registered_ip, status)
            VALUES (?, ?, ?, ?, ?, 'active')
        ''', (username, pwd_hash, salt, role, ip))
        user_id = cursor.lastrowid
        
        # Log registration IP
        cursor.execute('''
            INSERT INTO registration_logs (ip_address, username)
            VALUES (?, ?)
        ''', (ip, username))
        
        conn.commit()
        return user_id

def update_user_login(user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
        conn.commit()

# --- IP Anti-Abuse Checks ---
def check_ip_registration_allowed(ip: str) -> tuple[bool, str]:
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 1. Check if registration is enabled globally
        cursor.execute("SELECT value FROM settings WHERE key = 'allow_registration'")
        row = cursor.fetchone()
        if row and row["value"] != "1":
            return False, "系统当前已暂停开放新用户注册"
            
        # 2. Check max registrations for this IP
        cursor.execute("SELECT value FROM settings WHERE key = 'max_users_per_ip'")
        row = cursor.fetchone()
        max_allowed = int(row["value"]) if row else 3
        
        cursor.execute("SELECT COUNT(*) as count FROM registration_logs WHERE ip_address = ?", (ip,))
        curr_count = cursor.fetchone()["count"]
        if curr_count >= max_allowed:
            return False, f"该 IP 注册账号数量已达上限 (已注册 {curr_count} 个，上限 {max_allowed} 个)"
            
        # 3. Check cooldown interval
        cursor.execute("SELECT value FROM settings WHERE key = 'cooldown_seconds'")
        row = cursor.fetchone()
        cooldown = int(row["value"]) if row else 60
        
        cursor.execute('''
            SELECT (strftime('%s', 'now') - strftime('%s', registered_at)) as diff 
            FROM registration_logs 
            WHERE ip_address = ? 
            ORDER BY registered_at DESC LIMIT 1
        ''', (ip,))
        last_log = cursor.fetchone()
        if last_log and last_log["diff"] is not None and last_log["diff"] < cooldown:
            remaining = cooldown - int(last_log["diff"])
            return False, f"注册请求过于频繁，请在 {remaining} 秒后再试"
            
        return True, "OK"

# --- Device & Binding Queries ---
def upsert_device(mac: str, device_type: str, ip: str, default_name: str = None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices WHERE mac = ?", (mac,))
        row = cursor.fetchone()
        if row:
            target_ip = ip
            if ip == "127.0.0.1" and row["last_seen_ip"] and row["last_seen_ip"] != "127.0.0.1":
                target_ip = row["last_seen_ip"]
            cursor.execute('''
                UPDATE devices SET last_seen_ip = ?, last_seen_at = CURRENT_TIMESTAMP 
                WHERE mac = ?
            ''', (target_ip, mac))
        else:
            cursor.execute('''
                INSERT INTO devices (mac, device_type, custom_name, last_seen_ip, last_seen_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (mac, device_type, default_name or mac, ip))
        conn.commit()

def bind_device_to_user(mac: str, user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE devices SET bound_user_id = ?, bound_at = CURRENT_TIMESTAMP
            WHERE mac = ?
        ''', (user_id, mac))
        conn.commit()

def unbind_device(mac: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE devices SET bound_user_id = NULL, bound_at = NULL
            WHERE mac = ?
        ''', (mac,))
        conn.commit()

def get_devices_by_user(user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM devices WHERE bound_user_id = ? ORDER BY sort_order ASC, id ASC
        ''', (user_id,))
        return [dict(r) for r in cursor.fetchall()]

def update_user_devices_order(user_id: int, mac_list: list):
    with get_db() as conn:
        cursor = conn.cursor()
        for idx, mac in enumerate(mac_list):
            cursor.execute('''
                UPDATE devices SET sort_order = ? WHERE mac = ? AND bound_user_id = ?
            ''', (idx, mac.lower(), user_id))
        conn.commit()
        return True

def transfer_device(mac: str, target_username: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ?", (target_username.strip(),))
        user = cursor.fetchone()
        if not user:
            return False, "目标用户不存在"
        cursor.execute('''
            UPDATE devices SET bound_user_id = ?, bound_at = CURRENT_TIMESTAMP
            WHERE mac = ?
        ''', (user["id"], mac.lower()))
        conn.commit()
        return True, "设备已成功转移"

def get_all_devices():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT d.*, u.username as bound_username 
            FROM devices d 
            LEFT JOIN users u ON d.bound_user_id = u.id 
            ORDER BY d.sort_order ASC, d.id ASC
        ''')
        return [dict(r) for r in cursor.fetchall()]

def rename_device(mac: str, new_name: str, user_id: int = None, is_admin: bool = False):
    with get_db() as conn:
        cursor = conn.cursor()
        if is_admin:
            cursor.execute("UPDATE devices SET custom_name = ? WHERE mac = ?", (new_name, mac))
        else:
            cursor.execute("UPDATE devices SET custom_name = ? WHERE mac = ? AND bound_user_id = ?", (new_name, mac, user_id))
        conn.commit()
        return cursor.rowcount > 0

def get_device_by_mac(mac: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices WHERE mac = ?", (mac,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_device_plug_names(mac: str, plug_names_json: str, user_id: int = None, is_admin: bool = False):
    with get_db() as conn:
        cursor = conn.cursor()
        if is_admin:
            cursor.execute("UPDATE devices SET plug_names = ? WHERE mac = ?", (plug_names_json, mac))
        else:
            cursor.execute("UPDATE devices SET plug_names = ? WHERE mac = ? AND bound_user_id = ?", (plug_names_json, mac, user_id))
        conn.commit()
        return cursor.rowcount > 0

# --- Admin Queries (Supporting Large Scale Fleets) ---
def get_admin_stats():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE status = 'active'")
        active_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM devices")
        total_devices = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM devices WHERE bound_user_id IS NOT NULL")
        bound_devices = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM registration_logs WHERE date(registered_at) = date('now')")
        today_reg = cursor.fetchone()[0]
        return {
            "total_users": total_users,
            "active_users": active_users,
            "disabled_users": total_users - active_users,
            "total_devices": total_devices,
            "bound_devices": bound_devices,
            "unbound_devices": total_devices - bound_devices,
            "today_registrations": today_reg
        }

def get_paginated_users(search=None, status=None, page=1, page_size=10):
    with get_db() as conn:
        cursor = conn.cursor()
        conditions = []
        params = []
        if search:
            conditions.append("(u.username LIKE ? OR u.registered_ip LIKE ?)")
            s = f"%{search.strip()}%"
            params.extend([s, s])
        if status and status != "all":
            conditions.append("u.status = ?")
            params.append(status)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor.execute(f"SELECT COUNT(*) FROM users u {where_clause}", tuple(params))
        total = cursor.fetchone()[0]

        offset = max(0, (page - 1) * page_size)
        query = f'''
            SELECT u.id, u.username, u.role, u.registered_ip, u.created_at, u.last_login_at, u.status,
                   COUNT(d.id) as device_count,
                   GROUP_CONCAT(d.mac || '::' || d.device_type || '::' || COALESCE(d.custom_name, d.mac), ';;') as device_summary
            FROM users u
            LEFT JOIN devices d ON u.id = d.bound_user_id
            {where_clause}
            GROUP BY u.id
            ORDER BY u.id DESC
            LIMIT ? OFFSET ?
        '''
        cursor.execute(query, tuple(params + [page_size, offset]))
        rows = [dict(r) for r in cursor.fetchall()]
        return {"total": total, "page": page, "page_size": page_size, "users": rows}

def get_paginated_devices(search=None, device_type=None, bound_filter=None, page=1, page_size=10):
    with get_db() as conn:
        cursor = conn.cursor()
        conditions = []
        params = []
        if search:
            s = f"%{search.strip()}%"
            conditions.append("(d.mac LIKE ? OR d.custom_name LIKE ? OR u.username LIKE ? OR d.last_seen_ip LIKE ?)")
            params.extend([s, s, s, s])
        if device_type and device_type != "all":
            conditions.append("d.device_type = ?")
            params.append(device_type)
        if bound_filter == "bound":
            conditions.append("d.bound_user_id IS NOT NULL")
        elif bound_filter == "unbound":
            conditions.append("d.bound_user_id IS NULL")

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor.execute(f'''
            SELECT COUNT(*) FROM devices d
            LEFT JOIN users u ON d.bound_user_id = u.id
            {where_clause}
        ''', tuple(params))
        total = cursor.fetchone()[0]

        offset = max(0, (page - 1) * page_size)
        query = f'''
            SELECT d.*, u.username as bound_username
            FROM devices d
            LEFT JOIN users u ON d.bound_user_id = u.id
            {where_clause}
            ORDER BY d.id ASC
            LIMIT ? OFFSET ?
        '''
        cursor.execute(query, tuple(params + [page_size, offset]))
        rows = [dict(r) for r in cursor.fetchall()]
        return {"total": total, "page": page, "page_size": page_size, "devices": rows}

def get_all_users():
    return get_paginated_users(page=1, page_size=1000)["users"]

def update_user_status(user_id: int, status: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        conn.commit()

def reset_user_password(user_id: int, new_pwd: str):
    pwd_hash, salt = hash_password(new_pwd)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (pwd_hash, salt, user_id))
        conn.commit()

def delete_user(user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        # Unbind any devices first
        cursor.execute("UPDATE devices SET bound_user_id = NULL WHERE bound_user_id = ?", (user_id,))
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

def get_settings():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in cursor.fetchall()}

def update_setting(key: str, value: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
