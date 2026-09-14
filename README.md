# 校园广播 · 本地服务控制台

管理员在本机浏览器查看并管理三个后台服务：

| 服务 | 说明 | 健康检查端口 |
| --- | --- | --- |
| 定时播报 scheduled-broadcast | 按作息表调度铃声/广播 | 9101 |
| 音频同步 audio-sync | 向各教学楼终端同步音频库并校验 | 9102 |
| 设备心跳 device-heartbeat | 巡检功放、音柱等设备在线状态 | 9103 |

仅使用 Python 3 标准库，默认只监听 `127.0.0.1:8080`。

## 启动

```bash
python3 server.py                 # 默认 127.0.0.1:8080（可在 data/config.json 改）
python3 server.py --port 9000     # 临时指定
```

打开 http://127.0.0.1:8080 即可：查看运行状态/PID/运行时长、启动/停止服务、
实时查看各服务日志、查看历史操作记录（页面每 5 秒自动刷新状态与日志）。

## 目录约定

- `data/config.json` —— 配置（监听地址、端口、各服务运行参数）
- `data/state.json` —— 运行态（各服务 PID、启动时间，由管理台维护）
- `data/operations.jsonl` —— 操作记录（每次启动/停止成功或失败一行 JSON）
- `LOG/scheduled-broadcast.log`、`LOG/audio-sync.log`、`LOG/device-heartbeat.log` —— 服务日志
- `LOG/manager.log` —— 管理台自身日志
- `scripts/` —— 三个后台服务脚本与公共库 `common.py`

## HTTP 接口

- `GET  /api/status` —— 三个服务综合状态（PID + 健康端口探测）
- `POST /api/service/start`  body `{"key":"scheduled-broadcast"}`
- `POST /api/service/stop`   body `{"key":"scheduled-broadcast"}`
- `GET  /api/logs?key=audio-sync&lines=300`
- `GET  /api/operations?limit=50`
- `GET  /api/config`

## 错误码（三类环境问题提示互不相同）

| code | 触发条件 | 提示 |
| --- | --- | --- |
| `E_SCRIPT_MISSING` | `scripts/` 下服务脚本不存在 | 提示缺失脚本路径，请联系部署人员补齐 |
| `E_PORT_BUSY` | 健康检查端口被占用（启动前探测 / 子进程绑定失败） | 提示端口号，并通过 `/proc/net/tcp` 反查占用 PID |
| `E_PERMISSION` | 脚本不可读、`LOG/` 或 `data/` 不可写、系统拒绝发信号 | 提示具体缺失权限的对象与当前运行用户 |

另有 `E_ALREADY_RUNNING`、`E_NOT_RUNNING`、`E_UNKNOWN_SERVICE`、
`ERR_START_FAILED`、`ERR_STOP_FAILED` 等状态类错误。

## 停止服务的处理

先发送 `SIGTERM` 优雅停止（服务脚本捕获信号后写退出日志），等待 5 秒；
仍存活则对进程组发送 `SIGKILL` 强制结束，并复查端口是否已释放。
管理台自身停止（Ctrl+C）不会影响已启动的后台服务。
