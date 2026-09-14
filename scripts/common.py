# -*- coding: utf-8 -*-
"""校园广播本地服务 —— 后台服务公共库。

- SERVICES: 三个后台服务的元数据（名称、脚本、健康检查端口、日志文件）
- load_config: 读取 data/config.json
- write_log / _write_log: 向 LOG/<service-key>.log 追加日志
- health_server: 占用健康检查端口，返回 {"service","status","time"} JSON
- install_signals: 捕获 SIGTERM/SIGINT，切换 state["running"] 为 False
"""
import json
import os
import socket
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "LOG")
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

SERVICES = {
    "scheduled-broadcast": {
        "name": "定时播报",
        "script": "scripts/svc_scheduled_broadcast.py",
        "port": 9101,
        "log": "scheduled-broadcast.log",
    },
    "audio-sync": {
        "name": "音频同步",
        "script": "scripts/svc_audio_sync.py",
        "port": 9102,
        "log": "audio-sync.log",
    },
    "device-heartbeat": {
        "name": "设备心跳",
        "script": "scripts/svc_device_heartbeat.py",
        "port": 9103,
        "log": "device-heartbeat.log",
    },
}

DEFAULT_CONFIG = {
    "scheduled-broadcast": {"schedule_points": 8, "patrol_seconds": 15},
    "audio-sync": {"terminals": 5, "audio_files": 6, "interval_seconds": 12},
    "device-heartbeat": {"devices": 8, "period_seconds": 8},
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_log(key, message):
    """向 LOG/<key>.log 追加一行，时间格式 [2026/9/14 04:42:02]。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    line = "[%s] %s\n" % (
        datetime.now().strftime("%Y/%-m/%-d %H:%M:%S"),
        message,
    )
    with open(os.path.join(LOG_DIR, SERVICES[key]["log"]), "a", encoding="utf-8") as f:
        f.write(line)


def health_server(key, state):
    """在配置端口上提供 HTTP 健康检查（线程内运行，state['running']=False 时退出）。"""
    port = SERVICES[key]["port"]
    srvr = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srvr.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srvr.bind(("0.0.0.0", port))
    srvr.listen(8)
    srvr.settimeout(0.5)

    def reply(conn):
        try:
            conn.recv(4096)
            body = json.dumps(
                {
                    "service": key,
                    "status": "running",
                    "time": datetime.now().strftime("%H:%M:%S"),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() +
                b"\r\nConnection: close\r\n\r\n" + body
            )
        except OSError:
            pass
        finally:
            conn.close()

    while state["running"]:
        try:
            conn, _ = srvr.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=reply, args=(conn,), daemon=True).start()
    try:
        srvr.close()
    except OSError:
        pass


def install_signals(state, on_exit=None):
    import signal

    def _stop(signum, frame):
        state["running"] = False
        if on_exit:
            on_exit(signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
