# -*- coding: utf-8 -*-
"""设备心跳服务：监听健康检查端口 9103，按周期巡检已注册广播设备。"""
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (  # noqa: E402
    SERVICES,
    health_server,
    install_signals,
    load_config,
    _write_log,
)

KEY = "device-heartbeat"
DEVICES = [
    "功放-一号教学楼",
    "功放-二号教学楼",
    "功放-实验楼",
    "功放-教学楼B",
    "控制面板-广播室",
    "室外音柱-操场",
    "室外音柱-宿舍区",
    "分区控制器-主楼",
]
# 模拟偶发离线的设备（确定性序列，便于演示告警）
FLAP = ["", "", "功放-实验楼", "功放-教学楼B", "", "控制面板-广播室",
        "", "室外音柱-操场", "室外音柱-宿舍区", ""]


def log(msg):
    _write_log(KEY, msg)


def main():
    cfg = load_config().get(KEY, {})
    period = float(cfg.get("period_seconds", 8))
    total = int(cfg.get("devices", len(DEVICES)))
    devices = DEVICES[:total]

    state = {"running": True}
    install_signals(
        state,
        lambda signum: log("「设备心跳」收到停止信号，服务退出 PID=%d" % os.getpid()),
    )

    log("===== 管理员启动「设备心跳」 =====")
    log("已注册设备 %d 台，心跳周期 %.0f 秒" % (total, period))
    log("「设备心跳」服务就绪，健康检查端口 %d" % SERVICES[KEY]["port"])

    t = threading.Thread(target=health_server, args=(KEY, state), daemon=True)
    t.start()
    log("「设备心跳」启动成功，PID=%d，端口=%d" % (os.getpid(), SERVICES[KEY]["port"]))

    idx = 0
    while state["running"]:
        for _ in range(int(period * 10)):
            if not state["running"]:
                break
            time.sleep(0.1)
        if not state["running"]:
            break
        idx += 1
        offline = FLAP[(idx - 1) % len(FLAP)]
        if offline and offline in devices:
            online = total - 1
            log("心跳巡检：在线 %d/%d，离线：%s（已告警）" % (online, total, offline))
        else:
            log("心跳巡检：在线 %d/%d，全部正常" % (total, total))


if __name__ == "__main__":
    main()
