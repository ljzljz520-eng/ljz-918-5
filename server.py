# -*- coding: utf-8 -*-
"""校园广播本地服务 —— 管理服务。

面向管理员的本地 Web 控制台：
  * 查看「定时播报 / 音频同步 / 设备心跳」三个后台服务的运行状态
  * 启动 / 停止服务，查看实时日志
  * data/config.json   存配置
    data/state.json    存运行态（PID、启动时间）
    data/operations.jsonl 存全部操作记录
  * LOG/*.log          存各服务日志

仅监听 127.0.0.1，仅使用 Python 标准库。

启动：python3 server.py [--port 8080] [--host 127.0.0.1]
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
from common import SERVICES, LOG_DIR  # noqa: E402

DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
OPS_PATH = os.path.join(DATA_DIR, "operations.jsonl")
MANAGER_LOG = os.path.join(LOG_DIR, "manager.log")

DEFAULT_CONFIG = {"listen_host": "127.0.0.1", "listen_port": 8080}

# 错误码 —— 前端按 code 展示不同提示
ERR_SCRIPT_MISSING = "E_SCRIPT_MISSING"   # 服务脚本缺失
ERR_PORT_BUSY = "E_PORT_BUSY"             # 端口被占用
ERR_PERMISSION = "E_PERMISSION"           # 权限不足
ERR_ALREADY_RUNNING = "E_ALREADY_RUNNING"
ERR_NOT_RUNNING = "E_NOT_RUNNING"
ERR_UNKNOWN_SERVICE = "E_UNKNOWN_SERVICE"
ERR_START_FAILED = "ERR_START_FAILED"
ERR_STOP_FAILED = "ERR_STOP_FAILED"

_lock = threading.RLock()


# ---------------------------------------------------------------- 基础工具

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def manager_log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(MANAGER_LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (now_text(), msg))


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_CONFIG)


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def record_op(key, action, ok, message="", code=None, operator="admin"):
    """追加一条操作记录（启动/停止的成功与失败都记录）。"""
    entry = {
        "time": now_text(),
        "operator": operator,
        "service": key,
        "action": action,
        "result": "success" if ok else "failed",
        "code": code,
        "message": message,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OPS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        manager_log("写操作记录失败: %s" % exc)
    return entry


# ---------------------------------------------------------------- 进程/端口探测

def _can_read(path):
    """实际以只读方式打开一次，避免 os.access 被部分容器 DAC 拦截而误报。"""
    try:
        with open(path, "rb"):
            pass
    except PermissionError:
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _can_write_dir(path):
    """在目录下创建临时文件探测写权限（部分环境 os.access 不可靠）。"""
    probe = os.path.join(path, ".perm-probe")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except PermissionError:
        return False
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def pid_alive(pid):
    """进程是否存活。已退出但尚未回收的僵尸进程视为已结束。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # os.kill 对僵尸进程也会成功，需通过 /proc 判断状态
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8") as f:
            stat = f.read()
        # comm 字段可能含空格和括号，以最后一个 ')' 之后的内容为准
        rest = stat[stat.rfind(")") + 2:].split()
        if rest and rest[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def port_open(port, host="127.0.0.1", timeout=0.4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_port_owner(port):
    """通过 /proc/net/tcp 反查占用端口的进程 PID（仅 Linux 生效）。"""
    hex_port = "%04X" % port
    inode = None
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, "r", encoding="utf-8") as f:
                next(f)
                for line in f:
                    parts = line.split()
                    local = parts[1]
                    state = parts[3]
                    if state != "0A":  # LISTEN
                        continue
                    if local.split(":")[1] == hex_port:
                        inode = parts[9]
                        break
        except (OSError, StopIteration):
            continue
        if inode:
            break
    if not inode:
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % pid
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] == inode:
                    return int(pid)
        except OSError:
            continue
    return None


def health_check(key, port, timeout=0.7):
    """请求健康检查端口。返回 (identity, raw)；identity 为 JSON 中的 service 字段。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            chunks = []
            while True:
                buf = s.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
            raw = b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return None, None
    body = raw.split("\r\n\r\n", 1)[-1]
    try:
        data = json.loads(body)
        return data.get("service"), data
    except ValueError:
        return None, None


def tail_log(key, lines=200):
    path = os.path.join(LOG_DIR, SERVICES[key]["log"])
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except OSError:
        return []


def get_service_status(key):
    """综合 PID 与健康端口，给出单个服务的当前状态。"""
    meta = SERVICES[key]
    port = meta["port"]
    state = load_state()
    rec = state.get(key, {})
    pid = rec.get("pid")
    started_at = rec.get("started_at")
    alive = pid_alive(pid)
    identity, _ = health_check(key, port)

    info = {
        "key": key,
        "name": meta["name"],
        "port": port,
        "script": meta["script"],
        "log": meta["log"],
        "pid": pid if alive else None,
        "started_at": started_at if alive else None,
        "uptime": None,
        "running": False,
        "state": "stopped",
        "detail": "服务未运行",
    }

    if identity == key and alive:
        info["running"] = True
        info["state"] = "running"
        info["detail"] = "运行中"
        try:
            info["uptime"] = int(time.time() - datetime.strptime(
                started_at, "%Y-%m-%d %H:%M:%S").timestamp())
        except (TypeError, ValueError):
            pass
    elif identity == key and not alive:
        # 端口在正常应答但记录的 PID 已不在（不应出现），以端口为准并清理
        info["running"] = True
        info["state"] = "running"
        info["detail"] = "运行中（PID 记录失效，建议停止后重启）"
    elif identity and identity != key:
        info["state"] = "conflict"
        info["detail"] = "端口 %d 被其他服务（%s）占用" % (port, identity)
    elif port_open(port):
        owner = find_port_owner(port)
        who = ("，占用进程 PID=%s" % owner) if owner else ""
        info["state"] = "port_busy"
        info["detail"] = "端口 %d 已被外部程序占用%s" % (port, who)
    elif alive:
        info["state"] = "abnormal"
        info["detail"] = "进程存活（PID=%s）但健康检查端口无响应" % pid
    else:
        if pid:
            info["detail"] = "服务未运行（上次进程 PID=%s 已退出）" % pid
    return info


def reconcile_state():
    """清理已失效的 PID 记录。"""
    with _lock:
        state = load_state()
        changed = False
        for key in SERVICES:
            rec = state.get(key)
            if rec and not pid_alive(rec.get("pid")):
                manager_log("检测到「%s」进程 PID=%s 已退出，清理运行态"
                            % (SERVICES[key]["name"], rec.get("pid")))
                state[key] = {}
                changed = True
        if changed:
            save_state(state)


# ---------------------------------------------------------------- 启动 / 停止

def _read_startup_error(key):
    path = os.path.join(DATA_DIR, ".startup-%s.err" % key)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _classify_child_error(text):
    low = text.lower()
    if ("address already in use" in low or "errno 98" in low
            or "errno 48" in low or "10048" in low):
        return ERR_PORT_BUSY, "端口启动失败：地址已被占用"
    if ("permission denied" in low or "errno 13" in low
            or "operation not permitted" in low):
        return ERR_PERMISSION, "服务进程因权限不足启动失败"
    return ERR_START_FAILED, "服务进程异常退出"


def start_service(key):
    """启动服务。返回 (ok, code, message, detail)。"""
    meta = SERVICES[key]
    script_path = os.path.join(BASE_DIR, meta["script"])
    port = meta["port"]

    st = get_service_status(key)

    # 0) 已在运行
    if st["state"] == "running":
        return False, ERR_ALREADY_RUNNING, \
            "「%s」已在运行（PID=%s，端口 %d），请勿重复启动" \
            % (meta["name"], st["pid"], port), st["detail"]

    # 1) 脚本缺失
    if not os.path.isfile(script_path):
        return False, ERR_SCRIPT_MISSING, \
            "服务脚本缺失：%s 不存在，无法启动「%s」，请联系部署人员补齐脚本" \
            % (meta["script"], meta["name"]), \
            "expected %s" % script_path

    # 2) 权限不足（脚本不可读 / 日志或数据目录不可写）
    perm_targets = []
    if not _can_read(script_path):
        perm_targets.append("脚本文件 %s 不可读" % meta["script"])
    if not _can_write_dir(LOG_DIR):
        perm_targets.append("日志目录 LOG/ 不可写")
    if not _can_write_dir(DATA_DIR):
        perm_targets.append("数据目录 data/ 不可写")
    if perm_targets:
        return False, ERR_PERMISSION, \
            "权限不足：%s，请检查文件权限后重试（当前运行用户 %s）" \
            % ("；".join(perm_targets), _current_user()), ""

    # 3) 端口占用（区分本服务未跑但端口已被别的进程占用）
    if st["state"] in ("port_busy", "conflict"):
        owner = find_port_owner(port)
        extra = "（占用进程 PID=%s）" % owner if owner else "（无法定位占用进程，请检查本机程序）"
        return False, ERR_PORT_BUSY, \
            "端口 %d 已被占用%s，请先释放该端口再启动「%s」" \
            % (port, extra, meta["name"]), st["detail"]

    # 4) 拉起进程
    err_path = os.path.join(DATA_DIR, ".startup-%s.err" % key)
    try:
        errf = open(err_path, "w", encoding="utf-8")
    except OSError as exc:
        return False, ERR_PERMISSION, \
            "权限不足：无法写入启动诊断文件 data/.startup-%s.err（%s）" % (key, exc), ""
    try:
        proc = subprocess.Popen(
            [sys.executable, script_path],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=errf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except PermissionError as exc:
        errf.close()
        return False, ERR_PERMISSION, \
            "权限不足：操作系统拒绝创建服务进程（%s）" % exc, ""
    except OSError as exc:
        errf.close()
        return False, ERR_START_FAILED, "启动失败：%s" % exc, ""

    # 5) 等待健康端口就绪（最多约 4 秒），同时观察进程是否秒退
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if proc.poll() is not None:
            errf.close()
            err_text = _read_startup_error(key)
            code, hint = _classify_child_error(err_text)
            tail = "\n".join(err_text.strip().splitlines()[-3:])
            if code == ERR_PORT_BUSY:
                msg = "「%s」启动失败：端口 %d 被占用（子进程无法绑定）" % (meta["name"], port)
            elif code == ERR_PERMISSION:
                msg = "「%s」启动失败：服务进程权限不足，无法绑定端口或写日志" % meta["name"]
            else:
                msg = "「%s」启动失败：%s" % (meta["name"], hint)
            return False, code, msg, tail
        identity, _ = health_check(key, port, timeout=0.5)
        if identity == key:
            break
        time.sleep(0.2)
    else:
        errf.close()
        if port_open(port):
            owner = find_port_owner(port)
            extra = "（PID=%s）" % owner if owner else ""
            return False, ERR_PORT_BUSY, \
                "「%s」启动超时：端口 %d 被其他程序占用%s" % (meta["name"], port, extra), ""
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        return False, ERR_START_FAILED, \
            "「%s」启动超时：健康检查端口 %d 在 4 秒内未就绪，进程已终止" \
            % (meta["name"], port), _read_startup_error(key)

    errf.close()

    # 6) 记录运行态
    with _lock:
        state = load_state()
        state[key] = {"pid": proc.pid, "started_at": now_text()}
        try:
            save_state(state)
        except OSError as exc:
            manager_log("保存 state.json 失败: %s" % exc)

    return True, None, "「%s」启动成功（PID=%s，端口 %d）" \
        % (meta["name"], proc.pid, port), ""


def stop_service(key):
    meta = SERVICES[key]
    port = meta["port"]
    st = get_service_status(key)

    if st["state"] not in ("running", "abnormal"):
        if st["state"] in ("port_busy", "conflict"):
            return False, ERR_PORT_BUSY, \
                "端口 %d 被非本系统程序占用，管理台不能代为停止，请手动处理" % port, ""
        return False, ERR_NOT_RUNNING, \
            "「%s」当前未运行，无需停止" % meta["name"], ""

    pid = st["pid"] or load_state().get(key, {}).get("pid")
    if not pid:
        return False, ERR_NOT_RUNNING, "找不到「%s」的进程记录" % meta["name"], ""

    # 先 SIGTERM，等待 5 秒；仍不退出则 SIGKILL 整个进程组
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        return False, ERR_PERMISSION, \
            "权限不足：无权向 PID=%s 发送停止信号（%s），请用服务属主操作" % (pid, exc), ""

    deadline = time.time() + 5.0
    graceful = True
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.2)
    else:
        graceful = False
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.5)

    if pid_alive(pid):
        return False, ERR_STOP_FAILED, \
            "「%s」停止失败：PID=%s 仍存活，可能需要管理员手动结束" % (meta["name"], pid), ""

    with _lock:
        state = load_state()
        if key in state:
            state[key] = {}
            save_state(state)

    time.sleep(0.2)
    if port_open(port):
        return False, ERR_PORT_BUSY, \
            "「%s」进程已结束，但端口 %d 仍被占用，请检查残留进程" % (meta["name"], port), ""

    msg = "「%s」已停止（PID=%s，%s）" % (
        meta["name"], pid, "优雅退出" if graceful else "超时后强制结束")
    return True, None, msg, ""


def _current_user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    server_version = "CampusBroadcast/1.0"

    def log_message(self, fmt, *args):
        manager_log("%s - %s" % (self.address_string(), fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message, detail="", status=400):
        self._json({"ok": False, "error": {
            "code": code, "message": message, "detail": detail}}, status)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._page()
        if path == "/api/status":
            reconcile_state()
            svcs = {k: get_service_status(k) for k in SERVICES}
            logs_tail = {k: (tail_log(k, 1) or [""])[-1].strip()
                         for k in SERVICES}
            return self._json({"ok": True, "time": now_text(),
                               "services": svcs, "last_logs": logs_tail})
        if path == "/api/config":
            return self._json({"ok": True, "config": load_config()})
        if path == "/api/operations":
            return self._serve_operations()
        if path == "/api/logs":
            qs = self._query()
            key = qs.get("key", [""])[0]
            if key not in SERVICES:
                return self._fail(ERR_UNKNOWN_SERVICE, "未知的服务标识：%s" % key)
            try:
                limit = max(1, min(2000, int(qs.get("lines", ["200"])[0])))
            except ValueError:
                limit = 200
            return self._json({"ok": True, "key": key,
                               "lines": tail_log(key, limit)})
        return self._fail("E_NOT_FOUND", "页面或接口不存在：%s" % path, status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/service/start", "/api/service/stop"):
            return self._fail("E_NOT_FOUND", "接口不存在：%s" % path, status=404)
        action = "start" if path.endswith("/start") else "stop"
        data = self._read_json()
        key = (data or {}).get("key", "")
        if key not in SERVICES:
            record_op(key or "(空)", action, False, "未知服务标识", ERR_UNKNOWN_SERVICE)
            return self._fail(ERR_UNKNOWN_SERVICE, "未知的服务标识：%s" % key)

        with _lock:
            if action == "start":
                ok, code, msg, detail = start_service(key)
            else:
                ok, code, msg, detail = stop_service(key)
        record_op(key, action, ok, msg, code)
        manager_log("%s %s: %s" % (action, key, msg))
        if ok:
            return self._json({"ok": True, "message": msg,
                               "service": get_service_status(key)})
        http_status = 409 if code in (ERR_ALREADY_RUNNING, ERR_NOT_RUNNING,
                                      ERR_PORT_BUSY) else 400
        return self._fail(code, msg, detail, http_status)

    def _query(self):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if length <= 0 or length > 16384:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _serve_operations(self):
        qs = self._query()
        try:
            limit = max(1, min(500, int(qs.get("limit", ["50"])[0])))
        except ValueError:
            limit = 50
        entries = []
        try:
            with open(OPS_PATH, "r", encoding="utf-8") as f:
                entries = [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            pass
        return self._json({"ok": True, "records": entries[-limit:]})

    def _page(self):
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------- 前端页面

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>校园广播 · 本地服务控制台</title>
<style>
  :root { --bg:#f4f6fa; --card:#fff; --line:#e3e8f0; --txt:#1f2a3d;
          --muted:#6b7a90; --brand:#2f6fed; --ok:#1fa97a; --bad:#e0524d;
          --warn:#e8902a; --info:#4a7bd0; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
         background:var(--bg); color:var(--txt); }
  header { background:linear-gradient(120deg,#1f4fb0,#2f6fed); color:#fff;
           padding:18px 28px; display:flex; align-items:center; justify-content:space-between; }
  header h1 { font-size:19px; margin:0; font-weight:600; }
  header .meta { font-size:12.5px; opacity:.85; }
  main { max-width:1180px; margin:22px auto; padding:0 20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px; box-shadow:0 1px 3px rgba(30,50,90,.05); }
  .card h2 { margin:0 0 4px; font-size:16px; display:flex; align-items:center; gap:8px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .dot.running { background:var(--ok); box-shadow:0 0 0 4px rgba(31,169,122,.15); }
  .dot.stopped { background:#aab6c8; }
  .dot.abnormal,.dot.port_busy,.dot.conflict { background:var(--warn);
          box-shadow:0 0 0 4px rgba(232,144,42,.15); }
  .desc { color:var(--muted); font-size:12.5px; margin-bottom:12px; }
  .kv { display:grid; grid-template-columns:64px 1fr; gap:4px 10px; font-size:13px;
        margin-bottom:12px; }
  .kv .k { color:var(--muted); }
  .lastlog { font-size:12px; color:#42506a; background:#f6f8fc; border:1px solid var(--line);
             border-radius:8px; padding:8px 10px; min-height:34px; margin-bottom:12px;
             overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
  .btns { display:flex; gap:10px; }
  button { border:0; border-radius:8px; padding:8px 16px; font-size:13.5px;
           cursor:pointer; font-family:inherit; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .btn-start { background:var(--ok); color:#fff; }
  .btn-stop { background:var(--bad); color:#fff; }
  .btn-ghost { background:#eef2f9; color:var(--txt); }
  section.panel { margin-top:22px; background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:16px 18px; }
  section.panel h3 { margin:0 0 10px; font-size:15px; }
  .tabs { display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
  .tabs button.active { background:var(--brand); color:#fff; }
  pre.logbox { background:#0f1b33; color:#c9d6ef; font-size:12px; line-height:1.55;
          border-radius:10px; padding:12px 14px; max-height:340px; overflow:auto;
          margin:0; white-space:pre-wrap; word-break:break-all; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  .tag { padding:1px 8px; border-radius:10px; font-size:11.5px; }
  .tag.success { background:rgba(31,169,122,.12); color:var(--ok); }
  .tag.failed { background:rgba(224,82,77,.12); color:var(--bad); }
  #toast { position:fixed; top:18px; left:50%; transform:translateX(-50%);
           min-width:280px; max-width:560px; padding:11px 18px; border-radius:10px;
           color:#fff; font-size:13.5px; box-shadow:0 6px 20px rgba(0,0,0,.18);
           display:none; z-index:99; line-height:1.5; }
  #toast.ok { background:var(--ok); }
  #toast.err { background:var(--bad); }
  #toast.warn { background:var(--warn); }
  .hint { color:var(--muted); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<header>
  <h1>📢 校园广播 · 本地服务控制台</h1>
  <div class="meta">本机访问 · 配置 data/config.json · 日志 LOG/*.log · <span id="clock"></span></div>
</header>
<main>
  <div class="grid" id="cards"></div>

  <section class="panel">
    <h3>服务日志</h3>
    <div class="tabs" id="logtabs"></div>
    <pre class="logbox" id="logbox">点击下方服务页签加载日志…</pre>
    <div class="hint">每 5 秒自动刷新当前页签；日志文件位于 LOG/ 目录。</div>
  </section>

  <section class="panel">
    <h3>操作记录（data/operations.jsonl）</h3>
    <table>
      <thead><tr><th style="width:150px">时间</th><th style="width:90px">操作人</th>
        <th style="width:120px">服务</th><th style="width:70px">动作</th>
        <th style="width:70px">结果</th><th>说明</th></tr></thead>
      <tbody id="ops"><tr><td colspan="6" class="hint">加载中…</td></tr></tbody>
    </table>
  </section>
</main>
<div id="toast"></div>

<script>
const SERVICES = [
  {key:"scheduled-broadcast", name:"定时播报", desc:"按作息表触发铃声 / 广播，健康端口 9101"},
  {key:"audio-sync", name:"音频同步", desc:"向各教学楼终端同步音频库并校验，端口 9102"},
  {key:"device-heartbeat", name:"设备心跳", desc:"巡检功放、音柱等设备在线状态，端口 9103"}
];
const STATE_TEXT = {running:"运行中", stopped:"已停止", abnormal:"进程异常",
  port_busy:"端口被占用", conflict:"端口冲突"};
let currentLog = SERVICES[0].key;
let statusCache = {};

function toast(message, kind) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = kind || "ok";
  el.style.display = "block";
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.display = "none", 4200);
}
function fmtUptime(s) {
  if (s == null) return "—";
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), sec = s%60;
  return (h? h+"时":"") + (m? m+"分":"") + sec + "秒";
}

async function api(path, opts) {
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch(e) {}
  if (!resp.ok || data.ok === false) {
    const err = data.error || {code:"E_HTTP", message:"HTTP 请求失败（"+resp.status+"）"};
    throw err;
  }
  return data;
}

function renderCards(data) {
  const box = document.getElementById("cards");
  box.innerHTML = "";
  for (const meta of SERVICES) {
    const s = data.services[meta.key] || {};
    statusCache[meta.key] = s;
    const state = s.state || "stopped";
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h2><span class="dot ${state}"></span>${meta.name}</h2>
      <div class="desc">${meta.desc}</div>
      <div class="kv">
        <span class="k">状态</span><span>${STATE_TEXT[state] || state}${s.detail && state!=="running" ? "："+s.detail : ""}</span>
        <span class="k">端口</span><span>${meta.key === s.key ? s.port : "—"}</span>
        <span class="k">PID</span><span>${s.pid || "—"}</span>
        <span class="k">启动于</span><span>${s.started_at || "—"}</span>
        <span class="k">已运行</span><span>${fmtUptime(s.uptime)}</span>
      </div>
      <div class="lastlog" title="${(data.last_logs[meta.key]||"").replace(/"/g,"&quot;")}">
        ${data.last_logs[meta.key] || "暂无日志"}
      </div>
      <div class="btns">
        <button class="btn-start" ${state==="running"?"disabled":""}
          onclick="doAction('start','${meta.key}')">启动</button>
        <button class="btn-stop" ${state!=="running" && state!=="abnormal"?"disabled":""}
          onclick="doAction('stop','${meta.key}')">停止</button>
        <button class="btn-ghost" onclick="switchLog('${meta.key}')">查看日志</button>
      </div>`;
    box.appendChild(card);
  }
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    document.getElementById("clock").textContent = data.time;
    renderCards(data);
  } catch(e) { toast("状态获取失败：" + e.message, "err"); }
}

async function doAction(action, key) {
  const btnName = action === "start" ? "启动" : "停止";
  toast(btnName + "请求已提交…", "warn");
  try {
    const data = await api("/api/service/" + action, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key})
    });
    toast(data.message, "ok");
  } catch(e) {
    // 三类环境问题给出不同提示
    let prefix = "❌ ";
    if (e.code === "E_SCRIPT_MISSING") prefix = "📄 脚本缺失：";
    else if (e.code === "E_PORT_BUSY") prefix = "🔌 端口占用：";
    else if (e.code === "E_PERMISSION") prefix = "🔒 权限不足：";
    toast(prefix + e.message, "err");
  }
  await refreshStatus();
  loadOps();
  if (action === "start") loadLog(currentLog);
}

function buildTabs() {
  const tabs = document.getElementById("logtabs");
  for (const meta of SERVICES) {
    const b = document.createElement("button");
    b.className = "btn-ghost" + (meta.key === currentLog ? " active" : "");
    b.textContent = meta.name;
    b.onclick = () => switchLog(meta.key);
    b.dataset.key = meta.key;
    tabs.appendChild(b);
  }
}
function switchLog(key) {
  currentLog = key;
  document.querySelectorAll("#logtabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.key === key));
  loadLog(key);
}
async function loadLog(key) {
  try {
    const data = await api("/api/logs?key=" + encodeURIComponent(key) + "&lines=300");
    const box = document.getElementById("logbox");
    box.textContent = data.lines.join("") || "暂无日志";
    box.scrollTop = box.scrollHeight;
  } catch(e) {
    document.getElementById("logbox").textContent = "日志加载失败：" + e.message;
  }
}
async function loadOps() {
  try {
    const data = await api("/api/operations?limit=30");
    const tbody = document.getElementById("ops");
    if (!data.records.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="hint">暂无操作记录</td></tr>';
      return;
    }
    tbody.innerHTML = data.records.slice().reverse().map(r => `
      <tr>
        <td>${r.time}</td><td>${r.operator||""}</td>
        <td>${(SERVICES.find(s=>s.key===r.service)||{}).name || r.service}</td>
        <td>${r.action==="start"?"启动":"停止"}</td>
        <td><span class="tag ${r.result}">${r.result==="success"?"成功":"失败"}</span></td>
        <td>${r.message||""}${r.code?'<span class="hint"> ['+r.code+']</span>':""}</td>
      </tr>`).join("");
  } catch(e) { /* ignore */ }
}

buildTabs();
refreshStatus();
loadLog(currentLog);
loadOps();
setInterval(refreshStatus, 5000);
setInterval(() => loadLog(currentLog), 5000);
setInterval(loadOps, 10000);
</script>
</body>
</html>
"""


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description="校园广播本地服务控制台")
    parser.add_argument("--host", default=cfg.get("listen_host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(cfg.get("listen_port", 8080)))
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    manager_log("管理台启动，监听 http://%s:%d" % (args.host, args.port))
    print("校园广播本地服务控制台已启动：http://%s:%d" % (args.host, args.port))
    print("按 Ctrl+C 停止管理台（不会影响后台服务）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        manager_log("管理台停止")


if __name__ == "__main__":
    main()
