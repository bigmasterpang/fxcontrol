# Phicomm FXControl Hub (Open-Source Cloud IoT Platform)

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License MIT">
  <img src="https://img.shields.io/badge/Python-3.10%2B-green.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/MQTT-v3.1.1-orange.svg" alt="MQTT v3.1.1">
  <img src="https://img.shields.io/badge/Protocol-TCP%20%7C%20TLS%20Dual--Mode-brightgreen.svg" alt="Dual Mode">
  <img src="https://img.shields.io/badge/Docker-Supported-blue.svg" alt="Docker">
</p>

> [!IMPORTANT]
> ### ⚠️ Firmware Prerequisite (Must Flash Firmware First)
> This system is **built entirely on and compatible with the open-source [a2633063/zTC1](https://github.com/a2633063/zTC1) firmware** (or custom forks derived from it).
> **Your Phicomm TC1 power strip or Phicomm M1 air detector must be flashed with this firmware before connecting to this platform.**
> Stock factory firmwares cannot establish an MQTT connection due to proprietary cloud locks and encryption protocols.

**FXControl** is an open-source, multi-tenant cloud IoT control hub specifically engineered for **Phicomm smart home devices**, including the **Phicomm TC1 6-socket smart power strip** (zTC1), **Phicomm M1 air detector** (zM1), and custom ESP32/MQTT climate panels.

Featuring a modern responsive Glassmorphism web interface, multi-user isolation, LAN auto-discovery, hardware-level offline timers, live power & PM2.5 telemetry, and enterprise-grade fleet governance.

---

## 🌟 Key Features

* **Phicomm TC1 (zTC1) Power Strip Management**:
  * 6 individual relay controls with instant optimistic UI response.
  * Custom socket renaming with quick appliance tag suggestions.
  * **Chip-level Hardware Offline Timers**: 5 task slots burned directly into device flash; continues running accurately even during internet outages or reboots.
  * Real-time power wattage (W), running status, and cumulative power-on duration.
  * **📡 Real-Time MQTT Telemetry Logs**: Dedicated card button to view recent 30 MQTT messages (RX telemetry & TX commands) formatted in JSON.
* **Phicomm M1 (zM1) Air Quality Monitor**:
  * Live environment metrics: PM2.5 (μg/m³), Formaldehyde HCHO (mg/m³), Temperature (°C), and Humidity (%).
  * Remote display controls: Screen sleep/wake and 4 brightness levels.
  * **📡 Real-Time MQTT Telemetry Logs**: Dedicated card button to inspect recent sensor telemetry payloads.
* **Dual-Mode MQTT Multiplexer (HAProxy)**:
  * Seamlessly multiplexes plain TCP MQTT (ESP8266 TC1/M1) and TLS encrypted MQTTS (ESP32) on a single port using SNI/TLS header inspection (`req_ssl_hello_type 1`).
* **Multi-User Isolation & Anti-Abuse**:
  * JWT-based stateless authentication with per-user device isolation.
  * IP registration rate-limiting and registration toggle.
  * Automatic local network device discovery and batch claiming.
* **Fleet Administration Console**:
  * Dashboard metrics: total users, active devices, and live MQTT client sessions.
  * **Streamlined User Devices Inspection**: Clicking a user opens a concise overview of total devices, online/offline counts, and recent MQTT message summaries, with 1-click access to full communication logs.
  * Device re-assignment and forced unbinding.

---

## 🚀 Quick Start

### Option A: Docker Compose (Recommended)

```bash
# 1. Clone repository
git clone https://github.com/bigmasterpang/fxcontrol.git
cd fxcontrol

# 2. Copy and configure environment file
cp .env.example .env

# 3. Launch the container stack (App + Mosquitto Broker)
docker compose up -d --build

# Access UI: http://localhost:8089 or http://your-ip:8089/fx/
```

#### Environment Variables & Build Arguments (`.env`)

| Variable / Build Arg | Default | Description |
| :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | Backend bind host |
| `PORT` | `8089` | Backend web & API port |
| `JWT_SECRET` | *(Random)* | JWT signing secret for auth tokens |
| `MQTT_BROKER` | `mosquitto` | Internal MQTT broker hostname |
| `MQTT_PORT` | `1883` | Internal MQTT port |
| `MQTT_PUBLIC_HOST` | *(Empty)* | **Public/External MQTT host/domain shown to users**. If left empty, automatically detects current visitor's browser host. If configured, validates DNS resolution against server IP. |
| `MQTT_PUBLIC_PORT` | `1883` | Public MQTT port shown to users |
| `MQTT_USER` | `master` | MQTT authentication username |
| `MQTT_PASS` | `apple123` | MQTT authentication password |
| `DB_PATH` | `/app/data/fx.db` | SQLite database path (mounted to `./data`) |
| `FX_ADMIN_USER` | `admin` | Default admin username initialized on first run |
| `FX_ADMIN_PASS` | `admin123` | Default admin password |

---

### 🌐 Dynamic MQTT Guide & Domain DNS Verification

The platform setup guide on the login page features intelligent host detection:
1. **Dynamic Host Detection**: When `MQTT_PUBLIC_HOST` is left empty, the UI dynamically adopts the browser's current visiting host (IP or domain).
2. **Domain DNS Validation & IP Fallback (e.g. `vm.dapang.wang`)**:
   - If an administrator configures a custom domain (such as `vm.dapang.wang`, whose actual public IP resolves to `38.47.108.223`):
   - The backend checks whether the domain's DNS resolves to the current server's actual IP address.
   - **Matching IP**: If the resolved IP matches the server IP (e.g. running on the production server `38.47.108.223`), the guide card displays `vm.dapang.wang`.
   - **Mismatched IP**: If running on a different test server (such as test host `106.14.225.57` or local PVE), or if DNS resolution fails, the system **automatically falls back to the server's real IP** (e.g. displaying `106.14.225.57`) with an inline warning `(domain mismatch, reverted to server IP)`. This prevents IoT devices from failing to connect due to misconfigured domains.
3. **UDP Broadcast Provisioning Consistency**:
   - When configuring devices via local UDP broadcast (port 10182), the target `mqtt_uri` parameter **must strictly match the valid address (domain or fallback IP) displayed in the guide card above**.
4. **One-Click Copy & UDP Provisioning Payload**: Provides instant copy buttons for credentials and formats the exact UDP configuration JSON for device batch setup.

---

### 🔒 SSL / TLS Certificate Best Practices (External Nginx)

> **Recommendation**: **Terminate SSL / TLS on the external host Nginx rather than inside the Docker container.**

* **Zero-Downtime Renewals**: Automated Let's Encrypt renewal with Certbot / acme.sh only requires a millisecond `systemctl reload nginx`, with zero container downtime or device disconnections.
* **Separation of Concerns**: Containers remain focused solely on application and MQTT logic, keeping images clean and portable.

#### External Nginx Configuration Example

```nginx
# 1. Web & API HTTPS Proxy
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location /fx/ {
        proxy_pass http://127.0.0.1:8089;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_http_version 1.1;
    }
}
```

```nginx
# 2. MQTT TLS (8883) Termination (nginx stream module)
stream {
    server {
        listen 8883 ssl;
        ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

        proxy_pass 127.0.0.1:1883;
        proxy_timeout 1h;
        proxy_connect_timeout 10s;
    }
}
```

---

### Option B: Bare-Metal Python

```bash
pip install -r requirements.txt
python scripts/init_admin.py --username admin --password "admin123"
python server/app.py
```

---

## 📄 License

Distributed under the [MIT License](LICENSE).
