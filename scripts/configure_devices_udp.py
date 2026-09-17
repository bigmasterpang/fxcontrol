#!/usr/bin/env python3
"""
Phicomm LAN Device Batch Configuration Tool (UDP 10182)
Configures MQTT broker, port, and credentials for zTC1 and zM1 devices over local UDP.
"""
import socket
import json
import time
import argparse

def configure_devices(broker_host, broker_port, username, password, target_ips=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 10181))
    s.settimeout(2.0)

    setting = {
        "mqtt_uri": broker_host,
        "mqtt_port": int(broker_port),
        "mqtt_user": username,
        "mqtt_password": password
    }

    pkt = {"mac": "", "setting": setting}
    payload = json.dumps(pkt).encode('utf-8')

    destinations = target_ips if target_ips else ['192.168.8.255', '255.255.255.255']
    print(f"[*] Broadcasting configuration to {destinations}: {setting}")

    for ip in destinations:
        for _ in range(3):
            s.sendto(payload, (ip, 10182))
            time.sleep(0.1)

    print("[*] Listening for device ACK responses...")
    start_t = time.time()
    count = 0
    while time.time() - start_t < 4.0:
        try:
            resp, addr = s.recvfrom(4096)
            data = resp.decode('utf-8', errors='replace').strip()
            print(f"  [+] Device at {addr[0]}: {data}")
            count += 1
        except socket.timeout:
            break
        except Exception as e:
            print(f"  [-] Error: {e}")
            break

    s.close()
    print(f"[*] Done! Received responses from {count} device(s).")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Configure Phicomm zTC1 / zM1 via UDP broadcast")
    parser.add_argument("--broker", required=True, help="MQTT Broker hostname or IP")
    parser.add_argument("--port", type=int, default=1883, help="MQTT Broker port (default: 1883)")
    parser.add_argument("--user", default="", help="MQTT username (optional)")
    parser.add_argument("--password", default="", help="MQTT password (optional)")
    parser.add_argument("--ips", nargs="*", help="Specific target IP addresses")
    args = parser.parse_args()

    configure_devices(args.broker, args.port, args.user, args.password, args.ips)
