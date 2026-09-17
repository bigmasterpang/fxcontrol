import os
import re
import json
import time
import subprocess
import threading
import collections
from functools import wraps
from datetime import datetime, timedelta, timezone

# East 8 Timezone (UTC+8 / China Standard Time)
TZ_CST = timezone(timedelta(hours=8))

def get_cst_time_str(ts=None):
    if ts is not None:
        dt = datetime.fromtimestamp(ts, tz=TZ_CST)
    else:
        dt = datetime.now(TZ_CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

"""
FX Manager - Cloud Multi-User Device Management Backend
Flask API for Phicomm FX Device Control Hub.
"""
MQTT_USER = os.environ.get("MQTT_USER", "master")
MQTT_PASS = os.environ.get("MQTT_PASS", "apple123")

from flask import Flask, request, jsonify, g
import jwt
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

import models

app = Flask(__name__, static_folder="static", static_url_path="")

class PrefixMiddleware:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith("/fx/"):
            environ["PATH_INFO"] = path[3:]
        elif path == "/fx":
            environ["PATH_INFO"] = "/"
        return self.wsgi_app(environ, start_response)

app.wsgi_app = PrefixMiddleware(app.wsgi_app)

@app.route("/")
def serve_index():
    return app.send_static_file("index.html")

JWT_SECRET = os.environ.get("JWT_SECRET", "fxcontrol_default_secret_change_me_in_production")
JWT_ALGO = "HS256"

# In-memory telemetry and state cache from MQTT
telemetry_cache = {}  # {mac: {"pm25": ..., "temperature": ..., "humidity": ..., "formaldehyde": ..., "updated_at": ...}}
state_cache = {}      # {mac: {"plugs": [...], "version": ..., "updated_at": ...}}

# Ring buffer for recent MQTT messages (per device MAC, stores last 30 messages)
recent_mqtt_cache = collections.defaultdict(lambda: collections.deque(maxlen=30))

# MQTT Client
mqtt_client = None

def publish_mqtt_command(topic, payload_dict, mac=None):
    """Publishes an MQTT command and records it to recent_mqtt_cache."""
    global mqtt_client
    if not mac and isinstance(payload_dict, dict):
        mac = payload_dict.get("mac")
    if not mac:
        parts = topic.split("/")
        if len(parts) >= 3 and len(parts[2]) == 12:
            mac = parts[2]

    payload_str = json.dumps(payload_dict) if isinstance(payload_dict, dict) else str(payload_dict)
    if mqtt_client:
        try:
            mqtt_client.publish(topic, payload_str)
        except Exception as e:
            print(f"[FX-MQTT] Publish error: {e}")

    if mac:
        mac = mac.lower()
        now_ts = time.time()
        time_str = get_cst_time_str(now_ts)
        recent_mqtt_cache[mac].append({
            "time": time_str,
            "timestamp": now_ts,
            "direction": "tx",
            "topic": topic,
            "payload": payload_dict if isinstance(payload_dict, dict) else payload_str
        })


import ipaddress

def get_client_ip():
    """Extracts client IP considering Nginx reverse proxy headers."""
    ip = request.headers.get("X-Real-IP")
    if not ip:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            ip = xff.split(",")[0].strip()
    if not ip:
        ip = request.remote_addr
    return ip or "127.0.0.1"

def is_same_lan_or_ip(dev_ip, client_ip):
    """Checks if device and client share the same IP or belong to the same LAN subnet."""
    if not dev_ip or dev_ip == "--" or not client_ip:
        return True
    if dev_ip == client_ip:
        return True
    try:
        dip = ipaddress.ip_address(dev_ip)
        cip = ipaddress.ip_address(client_ip)
        if dip.is_private and cip.is_private:
            if dip.is_loopback or cip.is_loopback:
                return True
            dnet = ipaddress.ip_network(f"{dip}/24", strict=False)
            cnet = ipaddress.ip_network(f"{cip}/24", strict=False)
            return dnet == cnet
    except Exception:
        pass
    return False

# --- Authentication Decorators ---
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        elif request.cookies.get("fx_token"):
            token = request.cookies.get("fx_token")

        if not token:
            return jsonify({"success": False, "error": "请先登录", "code": "UNAUTHORIZED"}), 401

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
            user = models.get_user_by_id(payload["user_id"])
            if not user or user["status"] != "active":
                return jsonify({"success": False, "error": "账号已被禁用或不存在", "code": "USER_DISABLED"}), 403
            g.user = user
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "error": "登录已过期，请重新登录", "code": "TOKEN_EXPIRED"}), 401
        except Exception:
            return jsonify({"success": False, "error": "无效的身份凭证", "code": "INVALID_TOKEN"}), 401

        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not hasattr(g, "user") or g.user["role"] != "admin":
            return jsonify({"success": False, "error": "需要管理员权限", "code": "FORBIDDEN"}), 403
        return f(*args, **kwargs)
    return decorated

# --- MQTT Background Bridge ---
def init_mqtt_bridge():
    global mqtt_client
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("[FX-MQTT] Connected to Mosquitto broker on 127.0.0.1:1883", flush=True)
            client.subscribe("device/zm1/+/sensor")
            client.subscribe("device/zm1/+/state")
            client.subscribe("device/ztc1/+/state")
            client.subscribe("device/ztc1/+/sensor")
        else:
            print(f"[FX-MQTT] Connection returned result code {rc}", flush=True)

    def on_message(client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
            mac = payload.get("mac")
            if not mac:
                parts = topic.split("/")
                if len(parts) >= 3 and len(parts[2]) == 12:
                    mac = parts[2]
            if not mac:
                return

            mac = mac.lower()
            now_ts = time.time()
            time_str = get_cst_time_str(now_ts)

            # Record to recent_mqtt_cache
            recent_mqtt_cache[mac].append({
                "time": time_str,
                "timestamp": now_ts,
                "direction": "rx",
                "topic": topic,
                "payload": payload
            })

            if "zm1" in topic:
                if mac not in telemetry_cache:
                    telemetry_cache[mac] = {}
                raw_pm = payload.get("PM25") if payload.get("PM25") is not None else payload.get("pm25")
                if raw_pm is not None:
                    telemetry_cache[mac]["pm25"] = raw_pm
                if "temperature" in payload:
                    telemetry_cache[mac]["temperature"] = payload["temperature"]
                if "humidity" in payload:
                    telemetry_cache[mac]["humidity"] = payload["humidity"]
                if "formaldehyde" in payload:
                    telemetry_cache[mac]["formaldehyde"] = payload["formaldehyde"]
                if "brightness" in payload:
                    telemetry_cache[mac]["brightness"] = payload["brightness"]
                if "on" in payload:
                    telemetry_cache[mac]["screen_on"] = payload["on"]
                telemetry_cache[mac]["updated_at"] = now_ts
                telemetry_cache[mac]["last_heartbeat"] = now_ts

            elif "ztc1" in topic:
                if mac not in state_cache:
                    state_cache[mac] = {
                        "plugs": [{"id": i, "name": f"插孔 {i+1}", "on": False} for i in range(6)],
                        "settings": {},
                        "power": 0.0,
                        "total_time": 0,
                        "updated_at": now_ts,
                        "last_heartbeat": now_ts
                    }
                state_cache[mac]["last_heartbeat"] = now_ts

                if topic.endswith("/state"):
                    if "settings" not in state_cache[mac]:
                        state_cache[mac]["settings"] = {}
                    for i in range(6):
                        pkey = f"plug_{i}"
                        if pkey in payload:
                            pinfo = payload[pkey]
                            if isinstance(pinfo, dict):
                                if "on" in pinfo:
                                    state_cache[mac]["plugs"][i]["on"] = (pinfo["on"] == 1)
                                if "setting" in pinfo and pinfo["setting"]:
                                    state_cache[mac]["settings"][pkey] = pinfo["setting"]
                    if "name" in payload:
                        state_cache[mac]["device_name"] = payload["name"]
                    state_cache[mac]["updated_at"] = now_ts

                elif topic.endswith("/sensor"):
                    if "power" in payload:
                        try:
                            p_val = float(payload["power"])
                            state_cache[mac]["power"] = p_val if p_val >= 0 else 0.0
                        except Exception:
                            state_cache[mac]["power"] = payload["power"]
                    if "total_time" in payload:
                        state_cache[mac]["total_time"] = payload["total_time"]
                    state_cache[mac]["sensor_updated_at"] = now_ts
        except Exception as e:
            pass

    try:
        mqtt_broker = os.environ.get("MQTT_BROKER", "127.0.0.1")
        mqtt_port = int(os.environ.get("MQTT_PORT", 1883))
        mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="fx_cloud_bridge")
        
        user = MQTT_USER
        passwd = MQTT_PASS
        try:
            db_settings = models.get_settings()
            if db_settings.get("mqtt_user"):
                user = db_settings.get("mqtt_user")
            if db_settings.get("mqtt_pass"):
                passwd = db_settings.get("mqtt_pass")
        except Exception:
            pass

        if user and passwd:
            mqtt_client.username_pw_set(user, passwd)
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.connect(mqtt_broker, mqtt_port, 60)
        t = threading.Thread(target=mqtt_client.loop_forever, daemon=True)
        t.start()
    except Exception as e:
        print(f"[FX-MQTT] Failed to start MQTT bridge: {e}", flush=True)

# --- Active MQTT Connection Sniffer ---
def get_connected_mqtt_clients():
    """Finds active devices on port 1883/8883 and extracts their client ID and real IP."""
    # 1. Map HAProxy backend connections to client IP (if HAProxy multiplexer is active)
    haproxy_fd_to_ip = {}
    try:
        if os.path.exists("/run/haproxy/admin.sock"):
            sess_out = subprocess.check_output(
                "echo 'show sess' | socat stdio /run/haproxy/admin.sock",
                shell=True, timeout=2
            ).decode()
            for line in sess_out.splitlines():
                m_src = re.search(r'src=([\d\.]+):(\d+)', line)
                m_s1 = re.search(r's1=\[[^\]]*fd=(\d+)', line)
                if m_src and m_s1:
                    real_ip = m_src.group(1)
                    backend_fd = int(m_s1.group(1))
                    haproxy_fd_to_ip[backend_fd] = real_ip
    except Exception:
        pass

    # 2. Get local ports of HAProxy backend sockets
    local_port_to_ip = {}
    if haproxy_fd_to_ip:
        try:
            ss_ha = subprocess.check_output(
                "ss -tnp '( dport = :1883 or dport = :8884 )'",
                shell=True, timeout=2
            ).decode()
            for line in ss_ha.splitlines():
                m_port = re.search(r'127\.0\.0\.1:(\d+)\s+127\.0\.0\.1:(?:1883|8884)', line)
                m_fd = re.search(r'users:\(\("haproxy",pid=\d+,fd=(\d+)\)\)', line)
                if m_port and m_fd:
                    local_port = int(m_port.group(1))
                    fd = int(m_fd.group(1))
                    if fd in haproxy_fd_to_ip:
                        local_port_to_ip[local_port] = haproxy_fd_to_ip[fd]
        except Exception:
            pass

    # 3. Get active established connections on port 1883 and resolve real IP
    active_ports = {}  # (peer_ip, peer_port) -> real_ip
    try:
        ss_1883 = subprocess.check_output("ss -tnp sport = :1883", shell=True, timeout=2).decode()
        for line in ss_1883.splitlines():
            m = re.search(r':1883\s+([\d\.]+):(\d+)', line)
            if m:
                peer_ip = m.group(1)
                peer_port = int(m.group(2))
                if peer_ip == "127.0.0.1" and peer_port in local_port_to_ip:
                    active_ports[(peer_ip, peer_port)] = local_port_to_ip[peer_port]
                else:
                    active_ports[(peer_ip, peer_port)] = peer_ip
    except Exception:
        pass

    client_map = {}
    log_paths = ["/var/log/mosquitto/mosquitto.log", "/mosquitto/log/mosquitto.log"]
    log_file = next((p for p in log_paths if os.path.exists(p)), None)
    if log_file:
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.search(r'New client connected from ([\d\.]+):(\d+) as ([a-zA-Z0-9_\-]+)', line)
                    if m:
                        ip, port, client_id = m.group(1), int(m.group(2)), m.group(3).lower()
                        real_ip = active_ports.get((ip, port), ip)
                        dtype = None
                        dname = None
                        if client_id.startswith("b0f893"):
                            dtype = "zm1"
                            dname = "斐讯悟空 M1 空气检测仪"
                        elif client_id.startswith("d0bae4"):
                            dtype = "ztc1"
                            dname = "斐讯 TC1 智能排插"
                        elif len(client_id) == 12:
                            dtype = "unknown"
                            dname = f"智能设备 ({client_id[-4:]})"

                        if dtype:
                            client_map[client_id] = {
                                "mac": client_id,
                                "type": dtype,
                                "name": dname,
                                "ip": real_ip,
                                "port": port
                            }
        except Exception as e:
            print(f"[Detector] Read log error: {e}")

    # Also include devices that recently published messages into state_cache / telemetry_cache
    now_ts = time.time()
    for mac, st in state_cache.items():
        if now_ts - st.get("last_heartbeat", 0) < 120 and mac not in client_map:
            db_dev = models.get_device_by_mac(mac)
            ip = db_dev["last_seen_ip"] if (db_dev and db_dev.get("last_seen_ip")) else "127.0.0.1"
            client_map[mac] = {
                "mac": mac,
                "type": "ztc1",
                "name": db_dev["custom_name"] if (db_dev and db_dev.get("custom_name")) else "斐讯 TC1 智能排插",
                "ip": ip,
                "port": 1883
            }
    for mac, tel in telemetry_cache.items():
        if now_ts - tel.get("last_heartbeat", 0) < 120 and mac not in client_map:
            db_dev = models.get_device_by_mac(mac)
            ip = db_dev["last_seen_ip"] if (db_dev and db_dev.get("last_seen_ip")) else "127.0.0.1"
            client_map[mac] = {
                "mac": mac,
                "type": "zm1",
                "name": db_dev["custom_name"] if (db_dev and db_dev.get("custom_name")) else "斐讯悟空 M1 空气检测仪",
                "ip": ip,
                "port": 1883
            }

    # Upsert all found devices into database with real IP
    for mac, info in client_map.items():
        models.upsert_device(mac, info["type"], info["ip"], info["name"])

    return client_map

# =========================================================================
# API ROUTES
# =========================================================================

# --- 1. Auth APIs ---

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    client_ip = get_client_ip()

    if not username or len(username) < 3 or len(username) > 20:
        return jsonify({"success": False, "error": "用户名长度需在 3 到 20 个字符之间"}), 400
    if not re.match(r"^[a-zA-Z0-9_\u4e00-\u9fa5]+$", username):
        return jsonify({"success": False, "error": "用户名只允许包含字母、数字、下划线及中文"}), 400
    if not password or len(password) < 6:
        return jsonify({"success": False, "error": "密码长度不能少于 6 位"}), 400

    # IP anti-abuse rate limit check
    allowed, reason = models.check_ip_registration_allowed(client_ip)
    if not allowed:
        return jsonify({"success": False, "error": reason, "code": "IP_RATE_LIMITED"}), 429

    # Check unique username
    if models.get_user_by_username(username):
        return jsonify({"success": False, "error": "该用户名已被注册"}), 409

    try:
        user_id = models.create_user(username, password, client_ip, role="user")
        token = jwt.encode({
            "user_id": user_id,
            "username": username,
            "role": "user",
            "exp": datetime.utcnow() + timedelta(days=30)
        }, JWT_SECRET, algorithm=JWT_ALGO)

        return jsonify({
            "success": True,
            "token": token,
            "user": {"id": user_id, "username": username, "role": "user"}
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    user = models.get_user_by_username(username)
    if not user or not models.verify_password(password, user["salt"], user["password_hash"]):
        return jsonify({"success": False, "error": "用户名或密码错误"}), 401

    if user["status"] != "active":
        return jsonify({"success": False, "error": "该账号已被管理员禁用"}), 403

    models.update_user_login(user["id"])

    token = jwt.encode({
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": datetime.utcnow() + timedelta(days=30)
    }, JWT_SECRET, algorithm=JWT_ALGO)

    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"]
        }
    })

@app.route("/api/auth/me", methods=["GET"])
@login_required
def api_me():
    return jsonify({
        "success": True,
        "user": {
            "id": g.user["id"],
            "username": g.user["username"],
            "role": g.user["role"],
            "registered_ip": g.user["registered_ip"]
        }
    })

# --- 2. Discovery & Device Binding (方案 A) ---

@app.route("/api/discovery", methods=["GET"])
@login_required
def api_discovery():
    """Detects LAN devices matching visitor's IP and returns both candidates & bound devices."""
    client_ip = get_client_ip()
    user_id = g.user["id"]

    # 1. Detect all live devices in Mosquitto
    active_devices = get_connected_mqtt_clients()

    # 2. Get user's currently bound devices
    user_devices = models.get_devices_by_user(user_id)
    bound_macs = {d["mac"].lower() for d in user_devices}

    # Enrich user devices with live MQTT state & telemetry
    enriched_bound = []
    now_ts = time.time()
    for d in user_devices:
        mac = d["mac"].lower()
        dev_info = {
            "id": d["id"],
            "mac": mac,
            "type": d["device_type"],
            "name": d["custom_name"] or mac,
            "bound_at": d["bound_at"]
        }
        if d["device_type"] == "zm1":
            cached_m1 = telemetry_cache.get(mac, {})
            last_hb = cached_m1.get("last_heartbeat", 0)
            hb_sec = int(now_ts - last_hb) if last_hb else 9999
            dev_info["online"] = hb_sec < 120 or (mac in active_devices)
            dev_info["heartbeat_ago_sec"] = hb_sec
            dev_info["telemetry"] = {
                "pm25": cached_m1.get("pm25", "--"),
                "temperature": cached_m1.get("temperature", "--"),
                "humidity": cached_m1.get("humidity", "--"),
                "formaldehyde": cached_m1.get("formaldehyde", "--"),
                "brightness": cached_m1.get("brightness", 4),
                "screen_on": cached_m1.get("screen_on", 1),
                "updated_at": cached_m1.get("updated_at", 0)
            }
        elif d["device_type"] == "ztc1":
            cached_tc1 = state_cache.get(mac, {})
            last_hb = cached_tc1.get("last_heartbeat", 0)
            hb_sec = int(now_ts - last_hb) if last_hb else 9999
            dev_info["online"] = hb_sec < 120 or (mac in active_devices)
            dev_info["heartbeat_ago_sec"] = hb_sec

            # Load plug names
            stored_names = []
            if d.get("plug_names"):
                try:
                    stored_names = json.loads(d["plug_names"])
                except Exception:
                    pass
            if not stored_names or len(stored_names) < 6:
                if "48c0" in mac:
                    stored_names = ["电视机", "音响", "路由器", "机顶盒", "落地灯", "备用插座"]
                elif "2737" in mac:
                    stored_names = ["饮水机", "咖啡机", "微波炉", "电水壶", "餐边柜灯", "备用插孔"]
                else:
                    stored_names = [f"插孔 {i+1}" for i in range(6)]

            cached_plugs = cached_tc1.get("plugs", [])
            plugs = []
            for i in range(6):
                on_state = cached_plugs[i]["on"] if i < len(cached_plugs) else False
                plugs.append({
                    "id": i,
                    "name": stored_names[i] if i < len(stored_names) else f"插孔 {i+1}",
                    "on": on_state
                })

            dev_info["state"] = {
                "plugs": plugs,
                "plug_names": stored_names,
                "power": cached_tc1.get("power", 0.0),
                "total_time": cached_tc1.get("total_time", 0),
                "sensor_updated_at": cached_tc1.get("sensor_updated_at", 0),
                "timer_settings": cached_tc1.get("settings", {})
            }
        enriched_bound.append(dev_info)

    # 3. Find candidates: online devices sharing the SAME client IP that are NOT bound to current user
    candidate_devices = []
    all_db_devices = {r["mac"].lower(): r for r in models.get_all_devices()}
    seen_candidate_macs = set()

    for mac, info in active_devices.items():
        if is_same_lan_or_ip(info["ip"], client_ip) and mac not in bound_macs:
            seen_candidate_macs.add(mac)
            db_rec = all_db_devices.get(mac)
            is_bound_other = db_rec and db_rec["bound_user_id"] is not None and db_rec["bound_user_id"] != user_id
            candidate_devices.append({
                "mac": mac,
                "type": info["type"],
                "name": (db_rec["custom_name"] if (db_rec and db_rec["custom_name"]) else info["name"]),
                "ip": info["ip"],
                "is_bound_other": is_bound_other,
                "bound_username": (db_rec["bound_username"] if is_bound_other else None)
            })

    # Also include any unbound device from this client IP recorded in DB
    for mac, db_rec in all_db_devices.items():
        if mac not in bound_macs and mac not in seen_candidate_macs:
            if db_rec.get("bound_user_id") is None and is_same_lan_or_ip(db_rec.get("last_seen_ip"), client_ip):
                candidate_devices.append({
                    "mac": mac,
                    "type": db_rec["device_type"],
                    "name": db_rec["custom_name"] or f"智能设备 ({mac[-4:]})",
                    "ip": db_rec["last_seen_ip"],
                    "is_bound_other": False,
                    "bound_username": None
                })

    return jsonify({
        "success": True,
        "client_ip": client_ip,
        "bound_devices": enriched_bound,
        "candidate_devices": candidate_devices
    })

@app.route("/api/device/bind", methods=["POST"])
@login_required
def api_bind_device():
    data = request.get_json() or {}
    raw_mac = data.get("mac") or ""
    mac = re.sub(r"[^a-fA-F0-9]", "", raw_mac).lower()
    custom_name = (data.get("name") or "").strip()
    device_type = (data.get("device_type") or "").strip().lower()

    if not mac or len(mac) != 12:
        return jsonify({"success": False, "error": "请输入有效的 12 位 MAC 地址（例如 D0BAE46448C0）"}), 400

    client_ip = get_client_ip()
    user_id = g.user["id"]

    # Check device
    dev = models.get_device_by_mac(mac)
    if dev:
        if dev["bound_user_id"] and dev["bound_user_id"] != user_id:
            if g.user["role"] != "admin":
                return jsonify({"success": False, "error": "该设备已被其他账号绑定，需原机主解绑或联系管理员"}), 403
    else:
        # If device not yet in DB, auto-infer type & create record
        if not device_type or device_type not in ("ztc1", "zm1"):
            if mac.startswith("b0f893"):
                device_type = "zm1"
            elif mac.startswith("d0bae4"):
                device_type = "ztc1"
            else:
                device_type = "ztc1"

        default_name = custom_name or ("斐讯悟空 M1 空气检测仪" if device_type == "zm1" else "斐讯 TC1 智能排插")
        models.upsert_device(mac, device_type, client_ip, default_name)

    models.bind_device_to_user(mac, user_id)
    if custom_name:
        models.rename_device(mac, custom_name, user_id=user_id, is_admin=(g.user["role"] == "admin"))

    return jsonify({"success": True, "message": "设备绑定成功", "mac": mac})

@app.route("/api/device/bind_all", methods=["POST"])
@login_required
def api_bind_all_devices():
    client_ip = get_client_ip()
    user_id = g.user["id"]

    active_devices = get_connected_mqtt_clients()
    user_devices = models.get_devices_by_user(user_id)
    bound_macs = {d["mac"].lower() for d in user_devices}
    all_db_devices = {r["mac"].lower(): r for r in models.get_all_devices()}

    bound_count = 0
    newly_bound = []
    for mac, info in active_devices.items():
        if is_same_lan_or_ip(info["ip"], client_ip) and mac not in bound_macs:
            db_rec = all_db_devices.get(mac)
            if db_rec and db_rec["bound_user_id"] is not None and db_rec["bound_user_id"] != user_id:
                if g.user["role"] != "admin":
                    continue
            models.bind_device_to_user(mac, user_id)
            bound_count += 1
            newly_bound.append(mac)

    return jsonify({
        "success": True,
        "bound_count": bound_count,
        "bound_macs": newly_bound,
        "message": f"成功绑定 {bound_count} 台设备" if bound_count > 0 else "未发现可供绑定的新设备"
    })

@app.route("/api/device/reorder", methods=["POST"])
@login_required
def api_device_reorder():
    data = request.get_json() or {}
    order = data.get("order") or data.get("mac_list") or []
    if not isinstance(order, list):
        return jsonify({"success": False, "error": "order 必须是包含 MAC 的数组"}), 400

    models.update_user_devices_order(g.user["id"], order)
    return jsonify({"success": True, "message": "设备顺序已保存"})

@app.route("/api/device/unbind", methods=["POST"])
@login_required
def api_unbind_device():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    if not mac:
        return jsonify({"success": False, "error": "缺少设备 MAC"}), 400

    all_devs = {r["mac"].lower(): r for r in models.get_all_devices()}
    dev = all_devs.get(mac)
    if not dev:
        return jsonify({"success": False, "error": "设备不存在"}), 404

    if dev["bound_user_id"] != g.user["id"] and g.user["role"] != "admin":
        return jsonify({"success": False, "error": "无权解绑他人设备"}), 403

    client_ip = get_client_ip()
    models.unbind_device(mac)
    if client_ip and client_ip != "127.0.0.1":
        models.upsert_device(mac, dev["device_type"], client_ip, dev["custom_name"])
    return jsonify({"success": True, "message": "设备已成功解绑"})

@app.route("/api/device/rename", methods=["POST"])
@login_required
def api_rename_device():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    new_name = (data.get("name") or "").strip()
    if not mac or not new_name:
        return jsonify({"success": False, "error": "MAC 和名称不能为空"}), 400

    is_admin = g.user["role"] == "admin"
    ok = models.rename_device(mac, new_name, user_id=g.user["id"], is_admin=is_admin)
    if not ok:
        return jsonify({"success": False, "error": "重命名失败，设备可能未绑定到您账号"}), 403

    return jsonify({"success": True, "name": new_name})

@app.route("/api/device/plug_rename", methods=["POST"])
@login_required
def api_plug_rename():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    plug_index = data.get("plug_index")
    new_name = (data.get("name") or "").strip()

    if not mac or plug_index is None or not new_name:
        return jsonify({"success": False, "error": "MAC、插孔序号和名称不能为空"}), 400

    try:
        plug_idx = int(plug_index)
        if plug_idx < 0 or plug_idx > 5:
            return jsonify({"success": False, "error": "孔位序号须在 0 到 5 之间"}), 400
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "无效的孔位序号"}), 400

    dev = models.get_device_by_mac(mac)
    if not dev:
        return jsonify({"success": False, "error": "设备不存在"}), 404
    if dev["bound_user_id"] != g.user["id"] and g.user["role"] != "admin":
        return jsonify({"success": False, "error": "无权操作该设备"}), 403

    names = []
    if dev.get("plug_names"):
        try:
            names = json.loads(dev["plug_names"])
        except Exception:
            pass
    if not names or len(names) < 6:
        if "48c0" in mac:
            names = ["电视机", "音响", "路由器", "机顶盒", "落地灯", "备用插座"]
        elif "2737" in mac:
            names = ["饮水机", "咖啡机", "微波炉", "电水壶", "餐边柜灯", "备用插孔"]
        else:
            names = [f"插孔 {i+1}" for i in range(6)]

    names[plug_idx] = new_name[:12]
    models.update_device_plug_names(mac, json.dumps(names, ensure_ascii=False), g.user["id"], g.user["role"] == "admin")

    # Update in-memory state_cache
    if mac in state_cache and "plugs" in state_cache[mac]:
        if plug_idx < len(state_cache[mac]["plugs"]):
            state_cache[mac]["plugs"][plug_idx]["name"] = names[plug_idx]

    return jsonify({"success": True, "plug_names": names})

@app.route("/api/device/timer", methods=["POST"])
@login_required
def api_device_timer():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    action = data.get("action")  # 'set' | 'clear' | 'query'
    plug_index = int(data.get("plug_index", 0))
    slot = data.get("slot", "task_0")

    if not mac or not action:
        return jsonify({"success": False, "error": "缺少参数"}), 400

    dev = models.get_device_by_mac(mac)
    if not dev:
        return jsonify({"success": False, "error": "设备不存在"}), 404
    if dev["bound_user_id"] != g.user["id"] and g.user["role"] != "admin":
        return jsonify({"success": False, "error": "无权操作该设备"}), 403

    global mqtt_client
    if not mqtt_client:
        return jsonify({"success": False, "error": "MQTT 桥接未就绪"}), 500

    topic = f"device/ztc1/{mac}/set"
    pkey = f"plug_{plug_index}"

    if action == "query":
        publish_mqtt_command(topic, {"mac": mac, pkey: {"setting": None}}, mac)
        cached_setting = state_cache.get(mac, {}).get("settings", {}).get(pkey, {})
        return jsonify({"success": True, "setting": cached_setting})

    elif action == "set":
        hour = int(data.get("hour", 8))
        minute = int(data.get("minute", 0))
        repeat = int(data.get("repeat", 127))
        timer_action = int(data.get("timer_action", 1))

        cmd = {
            "mac": mac,
            pkey: {
                "setting": {
                    slot: {
                        "hour": hour,
                        "minute": minute,
                        "repeat": repeat,
                        "action": timer_action,
                        "on": 1
                    }
                }
            }
        }
        publish_mqtt_command(topic, cmd, mac)

        # Update cache
        if mac not in state_cache:
            state_cache[mac] = {"settings": {}}
        if "settings" not in state_cache[mac]:
            state_cache[mac]["settings"] = {}
        if pkey not in state_cache[mac]["settings"]:
            state_cache[mac]["settings"][pkey] = {}
        state_cache[mac]["settings"][pkey][slot] = {
            "hour": hour,
            "minute": minute,
            "repeat": repeat,
            "action": timer_action,
            "on": 1
        }
        return jsonify({"success": True, "message": "定时任务已成功下发至设备"})

    elif action == "clear":
        cmd = {
            "mac": mac,
            pkey: {
                "setting": {
                    slot: {
                        "on": 0
                    }
                }
            }
        }
        publish_mqtt_command(topic, cmd, mac)

        if mac in state_cache and "settings" in state_cache[mac] and pkey in state_cache[mac]["settings"]:
            if slot in state_cache[mac]["settings"][pkey]:
                state_cache[mac]["settings"][pkey][slot]["on"] = 0
        return jsonify({"success": True, "message": "定时任务已清除"})

    return jsonify({"success": False, "error": "未知 action"}), 400

# --- 3. Device Control API (MQTT Dispatched) ---

@app.route("/api/device/control", methods=["POST"])
@login_required
def api_device_control():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    action = data.get("action")

    if not mac or not action:
        return jsonify({"success": False, "error": "缺少 mac 或 action 参数"}), 400

    # Verify authorization
    all_devs = {r["mac"].lower(): r for r in models.get_all_devices()}
    dev = all_devs.get(mac)
    if not dev or (dev["bound_user_id"] != g.user["id"] and g.user["role"] != "admin"):
        return jsonify({"success": False, "error": "您未绑定该设备，无权控制"}), 403

    device_type = dev["device_type"]
    global mqtt_client

    if not mqtt_client:
        return jsonify({"success": False, "error": "服务器 MQTT 桥接服务未连接"}), 500

    start_t = time.time()

    if action == "reboot":
        topic = f"device/{device_type}/{mac}/set"
        cmd = {"mac": mac, "cmd": "restart", "action": "reboot", "reboot": 1}
        publish_mqtt_command(topic, cmd, mac)
        
        last_ip = dev.get("last_seen_ip")
        if last_ip and not last_ip.startswith("127."):
            def send_udp_reboot_bg(target_ip, target_mac):
                try:
                    import socket
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(0.5)
                    p_bytes = json.dumps({"mac": target_mac, "cmd": "restart", "action": "reboot", "reboot": 1}).encode('utf-8')
                    s.sendto(p_bytes, (target_ip, 10182))
                    s.close()
                except Exception:
                    pass
            threading.Thread(target=send_udp_reboot_bg, args=(last_ip, mac), daemon=True).start()

        latency = round((time.time() - start_t) * 1000, 1)
        return jsonify({"success": True, "latency_ms": latency, "message": "重启指令已发送"})

    if device_type == "ztc1":
        topic = f"device/ztc1/{mac}/set"
        if action == "set_plug":
            plug_id = int(data.get("plug_index", data.get("plug_id", 0)))
            raw_on = data.get("state", data.get("on"))
            on_val = 1 if (raw_on is True or raw_on == 1 or raw_on == "1") else 0
            cmd = {"mac": mac, f"plug_{plug_id}": {"on": on_val}}
            publish_mqtt_command(topic, cmd, mac)
            # Pre-update local cache
            if mac in state_cache and "plugs" in state_cache[mac]:
                for p in state_cache[mac]["plugs"]:
                    if p["id"] == plug_id:
                        p["on"] = bool(on_val)
        elif action == "set_all":
            raw_on = data.get("state", data.get("on"))
            on_val = 1 if (raw_on is True or raw_on == 1 or raw_on == "1") else 0
            cmd = {"mac": mac}
            for i in range(6):
                cmd[f"plug_{i}"] = {"on": on_val}
            publish_mqtt_command(topic, cmd, mac)
            if mac in state_cache and "plugs" in state_cache[mac]:
                for p in state_cache[mac]["plugs"]:
                    p["on"] = bool(on_val)
        elif action == "query":
            cmd = {"mac": mac, "version": 1}
            publish_mqtt_command(topic, cmd, mac)

    elif device_type == "zm1":
        topic = f"device/zm1/{mac}/set"
        if action == "set_brightness":
            val = max(0, min(4, int(data.get("brightness", 0))))
            cmd = {"mac": mac, "brightness": val}
            publish_mqtt_command(topic, cmd, mac)
            if mac in telemetry_cache:
                telemetry_cache[mac]["brightness"] = val
        elif action == "set_screen":
            raw_on = data.get("on", 1)
            on_val = 1 if (raw_on is True or raw_on == 1 or raw_on == "1") else 0
            cmd = {"mac": mac, "on": on_val}
            publish_mqtt_command(topic, cmd, mac)
            if mac in telemetry_cache:
                telemetry_cache[mac]["screen_on"] = on_val
        elif action == "query":
            cmd = {"mac": mac, "version": 1}
            publish_mqtt_command(topic, cmd, mac)

    latency = round((time.time() - start_t) * 1000, 1)
    return jsonify({"success": True, "latency_ms": latency})

@app.route("/api/device/mqtt_logs", methods=["GET"])
@login_required
def api_device_mqtt_logs():
    mac = request.args.get("mac", "").strip().lower()
    if not mac:
        return jsonify({"success": False, "error": "缺少 mac 参数"}), 400

    dev = models.get_device_by_mac(mac)
    if not dev:
        return jsonify({"success": False, "error": "未找到该设备"}), 404

    # Permission check: must be owner or admin
    if dev["bound_user_id"] != g.user["id"] and g.user["role"] != "admin":
        return jsonify({"success": False, "error": "无权查看该设备通信报文"}), 403

    logs = list(reversed(recent_mqtt_cache.get(mac, [])))
    return jsonify({
        "success": True,
        "mac": mac,
        "name": dev["custom_name"] or ("斐讯悟空 M1 空气检测仪" if dev["device_type"] == "zm1" else "斐讯 TC1 智能排插"),
        "device_type": dev["device_type"],
        "logs": logs,
        "total": len(logs)
    })


# --- 4. Admin APIs (Supporting Large Scale Fleets) ---

@app.route("/api/admin/overview", methods=["GET"])
@login_required
@admin_required
def api_admin_overview():
    stats = models.get_admin_stats()
    active = get_connected_mqtt_clients()
    stats["live_mqtt_clients"] = len(active)
    return jsonify({"success": True, "stats": stats})

@app.route("/api/admin/users", methods=["GET"])
@login_required
@admin_required
def api_admin_users():
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "all")
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, min(100, int(request.args.get("page_size", 10))))
    except ValueError:
        page, page_size = 1, 10
    data = models.get_paginated_users(search=search, status=status, page=page, page_size=page_size)
    return jsonify({"success": True, **data})

@app.route("/api/admin/user/devices", methods=["GET"])
@login_required
@admin_required
def api_admin_user_devices():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "缺少 user_id 参数"}), 400
    try:
        uid = int(user_id)
    except ValueError:
        return jsonify({"success": False, "error": "无效的 user_id"}), 400

    target_user = models.get_user_by_id(uid)
    if not target_user:
        return jsonify({"success": False, "error": "用户不存在"}), 404

    dev_rows = models.get_devices_by_user(uid)
    active = get_connected_mqtt_clients()
    now_ts = time.time()
    result = []
    online_count = 0
    for d in dev_rows:
        mac = d["mac"].lower()
        st = state_cache.get(mac, {})
        tel = telemetry_cache.get(mac, {})
        last_hb = st.get("last_heartbeat") or tel.get("last_heartbeat") or 0
        is_online = (now_ts - last_hb < 120) or (mac in active)
        if is_online:
            online_count += 1

        dev_logs = recent_mqtt_cache.get(mac, [])
        latest_mqtt = dev_logs[-1] if dev_logs else None

        result.append({
            "id": d["id"],
            "mac": mac,
            "device_type": d["device_type"],
            "name": d["custom_name"] or ("斐讯悟空 M1 空气检测仪" if d["device_type"] == "zm1" else "斐讯 TC1 智能排插"),
            "custom_name": d["custom_name"],
            "last_seen_ip": active[mac]["ip"] if (mac in active and active[mac].get("ip")) else (d.get("last_seen_ip") or "--"),
            "bound_at": d.get("bound_at"),
            "is_online": is_online,
            "last_heartbeat_ago": int(now_ts - last_hb) if last_hb else None,
            "latest_mqtt": latest_mqtt,
            "mqtt_log_count": len(dev_logs)
        })
    return jsonify({
        "success": True,
        "user": {
            "id": target_user["id"],
            "username": target_user["username"],
            "role": target_user["role"],
            "status": target_user["status"],
            "created_at": target_user["created_at"] if "created_at" in target_user.keys() else None
        },
        "stats": {
            "total_devices": len(dev_rows),
            "online_devices": online_count,
            "offline_devices": len(dev_rows) - online_count
        },
        "devices": result
    })

@app.route("/api/admin/user/status", methods=["POST"])
@login_required
@admin_required
def api_admin_user_status():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    status = data.get("status")
    if not user_id or status not in ("active", "disabled"):
        return jsonify({"success": False, "error": "参数错误"}), 400
    if user_id == g.user["id"]:
        return jsonify({"success": False, "error": "不能禁用当前管理员自己"}), 400
    models.update_user_status(user_id, status)
    return jsonify({"success": True})

@app.route("/api/admin/user/reset_pwd", methods=["POST"])
@login_required
@admin_required
def api_admin_reset_pwd():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    new_pwd = (data.get("new_password") or "").strip()
    if not user_id or len(new_pwd) < 6:
        return jsonify({"success": False, "error": "新密码不能少于 6 位"}), 400
    models.reset_user_password(user_id, new_pwd)
    return jsonify({"success": True, "message": "密码重置成功"})

@app.route("/api/admin/user/delete", methods=["POST"])
@login_required
@admin_required
def api_admin_delete_user():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "参数错误"}), 400
    if user_id == g.user["id"]:
        return jsonify({"success": False, "error": "不能删除自己"}), 400
    models.delete_user(user_id)
    return jsonify({"success": True, "message": "用户已删除"})

@app.route("/api/admin/devices", methods=["GET"])
@login_required
@admin_required
def api_admin_devices():
    search = request.args.get("search", "").strip()
    dtype = request.args.get("type", "all")
    bound_filter = request.args.get("bound", "all")
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = max(1, min(100, int(request.args.get("page_size", 10))))
    except ValueError:
        page, page_size = 1, 10

    active = get_connected_mqtt_clients()
    data = models.get_paginated_devices(search=search, device_type=dtype, bound_filter=bound_filter, page=page, page_size=page_size)
    now_ts = time.time()
    for dev in data["devices"]:
        mac = dev["mac"].lower()
        cached = state_cache.get(mac) or telemetry_cache.get(mac) or {}
        last_hb = cached.get("last_heartbeat", 0)
        dev["is_online"] = (now_ts - last_hb < 120) or (mac in active)
        dev["last_heartbeat_ago"] = int(now_ts - last_hb) if last_hb else None
        if mac in active and active[mac].get("ip"):
            dev["last_seen_ip"] = active[mac]["ip"]

    return jsonify({"success": True, **data})

@app.route("/api/admin/device/unbind", methods=["POST"])
@login_required
@admin_required
def api_admin_unbind_device():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    if not mac:
        return jsonify({"success": False, "error": "缺少 MAC"}), 400
    models.unbind_device(mac)
    return jsonify({"success": True, "message": "设备已成功解绑"})

@app.route("/api/admin/device/transfer", methods=["POST"])
@login_required
@admin_required
def api_admin_device_transfer():
    data = request.get_json() or {}
    mac = (data.get("mac") or "").strip().lower()
    target_username = (data.get("target_username") or "").strip()
    if not mac or not target_username:
        return jsonify({"success": False, "error": "MAC 和目标用户名不能为空"}), 400
    ok, msg = models.transfer_device(mac, target_username)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    return jsonify({"success": True, "message": msg})

@app.route("/api/public/config", methods=["GET"])
def api_public_config():
    import socket
    db_settings = models.get_settings()
    configured_host = (db_settings.get("mqtt_public_host") or os.environ.get("MQTT_PUBLIC_HOST", "")).strip()
    public_port = db_settings.get("mqtt_public_port") or os.environ.get("MQTT_PUBLIC_PORT") or os.environ.get("MQTT_PORT", "1883")
    mqtt_user = db_settings.get("mqtt_user") or os.environ.get("MQTT_USER", "")
    mqtt_pass = db_settings.get("mqtt_pass") or os.environ.get("MQTT_PASS", "")

    req_host = (request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host or "").split(":")[0].strip()

    final_host = configured_host
    domain_mismatch = False
    resolved_ip = None

    # Detect current server candidate IPs
    server_ips = []
    if req_host:
        try:
            ipaddress.ip_address(req_host)
            server_ips.append(req_host)
        except ValueError:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        sock_ip = s.getsockname()[0]
        s.close()
        if sock_ip and not sock_ip.startswith("127.") and sock_ip not in server_ips:
            server_ips.append(sock_ip)
    except Exception:
        pass

    primary_server_ip = server_ips[0] if server_ips else (req_host or "127.0.0.1")

    if configured_host:
        is_ip = False
        try:
            ipaddress.ip_address(configured_host)
            is_ip = True
        except ValueError:
            is_ip = False

        if not is_ip:
            # It's a domain name. Resolve it and verify if it matches current server IP
            try:
                resolved_ip = socket.gethostbyname(configured_host)
            except Exception:
                resolved_ip = None

            matched = False
            if resolved_ip:
                if resolved_ip in server_ips:
                    matched = True
                elif req_host:
                    try:
                        if resolved_ip == socket.gethostbyname(req_host):
                            matched = True
                    except Exception:
                        pass

            if not matched:
                domain_mismatch = True
                final_host = primary_server_ip

    return jsonify({
        "success": True,
        "mqtt": {
            "host": final_host,
            "configured_host": configured_host,
            "resolved_ip": resolved_ip,
            "server_ip": primary_server_ip,
            "domain_mismatch": domain_mismatch,
            "port": int(public_port) if str(public_port).isdigit() else 1883,
            "username": mqtt_user,
            "password": mqtt_pass,
            "has_auth": bool(mqtt_user or mqtt_pass)
        }
    })

@app.route("/api/admin/settings", methods=["GET", "POST"])
@login_required
@admin_required
def api_admin_settings():
    if request.method == "GET":
        settings = models.get_settings()
        if "mqtt_public_host" not in settings:
            settings["mqtt_public_host"] = os.environ.get("MQTT_PUBLIC_HOST", "")
        if "mqtt_public_port" not in settings:
            settings["mqtt_public_port"] = os.environ.get("MQTT_PUBLIC_PORT", os.environ.get("MQTT_PORT", "1883"))
        if "mqtt_user" not in settings:
            settings["mqtt_user"] = os.environ.get("MQTT_USER", "")
        if "mqtt_pass" not in settings:
            settings["mqtt_pass"] = os.environ.get("MQTT_PASS", "")
        return jsonify({"success": True, "settings": settings})
    else:
        data = request.get_json() or {}
        for k, v in data.items():
            models.update_setting(k, v)
        return jsonify({"success": True, "settings": models.get_settings()})

# Initialize DB and MQTT on start
models.init_db(os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "fx.db")))
init_mqtt_bridge()

if __name__ == "__main__":
    from waitress import serve
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8089))
    print(f"Starting FX Manager on {host}:{port}...")
    serve(app, host=host, port=port)
