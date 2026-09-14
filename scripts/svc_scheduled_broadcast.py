# -*- coding: utf-8 -*-
"""定时播报服务：监听健康检查端口 9101，按作息表模拟调度巡检。"""
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

KEY = "scheduled-broadcast"
SCHEDULE = [
    ("07:30", "早读铃声"),
    ("08:00", "第一节课上课铃"),
    ("08:45", "第一节课下课铃"),
    ("10:00", "眼保健操"),
    ("12:00", "午间音乐"),
    ("14:00", "下午上课铃"),
    ("17:30", "放学铃声"),
    ("21:00", "熄灯提醒"),
]


def log(msg):
    _write_log(KEY, msg)


def main():
    cfg = load_config().get(KEY, {})
    patrol_seconds = int(cfg.get("patrol_seconds", 15))

    state = {"running": True}
    install_signals(
        state,
        lambda signum: log("「定时播报」收到停止信号，服务退出 PID=%d" % os.getpid()),
    )

    log("===== 管理员启动「定时播报」 =====")
    log("作息表已加载，共 %d 个播报点" % len(SCHEDULE))
    log("「定时播报」服务就绪，健康检查端口 %d" % SERVICES[KEY]["port"])

    t = threading.Thread(target=health_server, args=(KEY, state), daemon=True)
    t.start()
    log("「定时播报」启动成功，PID=%d，端口=%d" % (os.getpid(), SERVICES[KEY]["port"]))

    round_idx = 0
    while state["running"]:
        for _ in range(patrol_seconds * 10):
            if not state["running"]:
                break
            time.sleep(0.1)
        if not state["running"]:
            break
        round_idx += 1
        now = datetime.now().strftime("%H:%M")
        nxt = next((t_ for t_, _ in SCHEDULE if t_ > now), "今日播报已结束")
        log(
            "调度器巡检：当前 %s，下一播报点 %s（第 %d 次巡检）"
            % (now, nxt, round_idx)
        )


if __name__ == "__main__":
    main()
