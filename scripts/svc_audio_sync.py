# -*- coding: utf-8 -*-
"""音频同步服务：监听健康检查端口 9102，模拟向各终端同步音频库文件。"""
import itertools
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

KEY = "audio-sync"
TERMINALS = ["一号教学楼", "二号教学楼", "实验楼", "宿舍楼", "操场广播室"]
AUDIO_FILES = [
    "bell-up.mp3",
    "bell-down.mp3",
    "exercise.mp3",
    "notice-bgm.mp3",
    "flag-raising.mp3",
    "emergency-notice.mp3",
]
SIZES_MB = [2.8, 3.4, 3.7, 4.2, 5.0, 5.6, 6.3, 7.3, 8.4, 8.7, 8.9]


def log(msg):
    _write_log(KEY, msg)


def main():
    cfg = load_config().get(KEY, {})
    interval = float(cfg.get("interval_seconds", 12))
    terminals = TERMINALS[: int(cfg.get("terminals", len(TERMINALS)))]
    files = AUDIO_FILES[: int(cfg.get("audio_files", len(AUDIO_FILES)))]

    state = {"running": True}
    install_signals(
        state,
        lambda signum: log("「音频同步」收到停止信号，服务退出 PID=%d" % os.getpid()),
    )

    log("===== 管理员启动「音频同步」 =====")
    log("同步目标 %d 个终端，音频库 %d 个文件" % (len(terminals), len(files)))
    log("「音频同步」服务就绪，健康检查端口 %d" % SERVICES[KEY]["port"])

    t = threading.Thread(target=health_server, args=(KEY, state), daemon=True)
    t.start()
    log("「音频同步」启动成功，PID=%d，端口=%d" % (os.getpid(), SERVICES[KEY]["port"]))

    combos = itertools.cycle(itertools.product(files, terminals))
    idx = 0
    while state["running"]:
        for _ in range(int(interval * 10)):
            if not state["running"]:
                break
            time.sleep(0.1)
        if not state["running"]:
            break
        idx += 1
        fname, terminal = next(combos)
        size = SIZES_MB[(idx - 1) % len(SIZES_MB)]
        stamp = datetime.now().strftime("%H:%M:%S")
        log(
            "同步 %s（%.1fMB）→ %s 终端 … 完成，校验一致 [%s]"
            % (fname, size, terminal, stamp)
        )


if __name__ == "__main__":
    main()
