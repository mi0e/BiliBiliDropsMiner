# Bilibili 直播掉宝助手

B 站直播掉宝 / 观看时长任务的自动挂机工具，支持 CLI 和 GUI 两种模式。

## 功能

- 自动连接直播间并维持 WebSocket + x25Kn 观看时长心跳
- 支持多房间同时挂机
- 任务进度实时追踪与可视化（分组进度条）
- 任务完成通知推送（企业微信、Gotify、Server 酱等）
- 运行参数动态修改，无需重启即可生效
- GUI / CLI 双模式，支持打包为独立 EXE

> **关于多线程（每房间多会话）：** B 站已在服务端按 UID 维度去重观看时长，多会话不再能叠加计时。该参数保留但已标注失效。

## 安装

```bash
pip install -r requirements.txt
```

依赖：httpx、websockets、brotli、colorama、customtkinter、apprise

## 使用方法

### GUI 模式（推荐）

```bash
python bilibili_gui.py
```

填入 Cookie、房间号、任务 ID 后点击「启动」即可。

### CLI 模式

```bash
python bilibili.py \
  --cookie "SESSDATA=xxx; bili_jct=xxx" \
  --rooms "23612045" \
  --task-ids "taskId1,taskId2" \
  -v
```

<details>
<summary>CLI 完整参数列表</summary>

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--cookie` | B 站登录 Cookie | 必填 |
| `--rooms` | 房间号，逗号分隔 | 必填 |
| `--threads` | 每房间会话数（已失效） | 1 |
| `--heartbeat-interval` | WS 心跳间隔（秒） | 30 |
| `--reconnect-delay` | 断线重连延迟（秒） | 8 |
| `--task-ids` | 任务 ID 列表，逗号分隔 | 空 |
| `--task-interval` | 任务进度查询间隔（秒） | 30 |
| `--notify-urls` | Apprise 通知 URL，逗号分隔 | 空 |
| `--disable-web-heartbeat` | 关闭 x25Kn 观看时长心跳 | false |
| `--disable-task-notify` | 关闭任务完成通知 | false |
| `-v` / `--verbose` | 显示详细调试日志 | false |

</details>

## 如何获取参数

### Cookie

浏览器登录 B 站 → F12 开发者工具 → 网络 / Network → 任意请求的 `Cookie` 请求头。

需要包含 `SESSDATA`、`bili_jct`、`DedeUserID` 等字段。

### 房间号

直播间 URL 中的数字，如 `https://live.bilibili.com/23612045` → 房间号为 `23612045`。

### 任务 ID

1. 前往 B 站活动任务页面
2. F12 开发者工具 → 网络 / Network
3. 点击页面上的刷新按钮，找到如下请求：
   ```
   https://api.bilibili.com/x/task/totalv2?csrf=xxx&task_ids=taskId1,taskId2,...&web_location=0.0
   ```
4. 从 `task_ids` 参数中提取，逗号分隔填入即可

## 通知推送

基于 [Apprise](https://github.com/caronc/apprise)，支持 80+ 通知平台。常用示例：

| 平台 | URL 格式 |
|---|---|
| 企业微信 | `wxwork://corpid/agentid/secret/?to=@all` |
| Gotify | `gotify://host/token` |
| Server 酱 | `schan://SendKey` |

多个通知地址用逗号分隔。

## 打包 EXE

```bash
python build.py              # 开发模式（目录）
python build.py --release     # 发布模式（单文件）
python build.py --target gui  # 仅打包 GUI
```

## 配置文件

GUI 支持保存 / 加载 JSON 配置文件，格式参考 [`config.example.json`](config.example.json)。

## License

MIT
