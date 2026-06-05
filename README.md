# Bilibili 直播掉宝助手

轻量、直接的 B 站直播掉宝/观看时长任务挂机工具。  
支持 GUI 与 CLI 双模式，支持多房间、多会话并行与任务进度追踪。

- [Release 下载](https://github.com/mi0e/BiliBiliDropsMiner/releases/latest)
- [国内下载（密码 1234）](https://wwaqd.lanzoum.com/b019vsjd5i)

## 🎈 界面预览

<details>
<summary>点击展开 GUI 截图</summary>

![GUI 截图](img/image_5.png)

</details>

## 🛠️ 功能

- 多房间并发挂机，支持每房间多会话连接
- 任务进度自动轮询 + 手动刷新
- 支持 Gotify、Server 酱通知
- GUI 支持配置保存/加载、日志查看、自动获取 Cookie 与任务 ID

## ⚠️ 免责声明

> [!IMPORTANT]
> **Disclaimer / 免责声明**
> - 本项目仅供个人学习研究，不保证稳定性，不提供技术支持
> - 使用本项目产生的一切后果由用户自行承担
> - 禁止商业用途，请遵守版权及平台规定
> - This project is for **personal learning and research purposes only**
> - No stability guarantee or technical support provided
> - Users are solely responsible for any consequences of using this project
> - Commercial use is strictly prohibited
> - Please respect copyright and platform ToS

## 🔍 参数获取指南

### Cookie（必填）

方式 1（推荐）：GUI 中点击“自动获取”，在弹出的浏览器中登录 B 站。  
方式 2（手动）：登录 B 站后 F12 打开开发者工具复制 Cookie，必需包含 `SESSDATA` 和 `bili_jct`，具体方法自行查询。

### 房间号（必填）

方式 1（推荐）：点击自动获取任务 ID 后进入直播间，房间号会自动回填。  
方式 2（手动）：直播间 URL 中的数字部分即为房间号，例如 `https://live.bilibili.com/23612045` 中房间号为 `23612045`。


### 任务 ID（可选）

方式 1（推荐）：GUI 中点击“自动获取”，进入活动直播间后自动回填。  
方式 2（手动）：在任务接口请求中提取 task_ids 参数。

典型请求示例：

```text
https://api.bilibili.com/x/task/totalv2?csrf=xxx&task_ids=taskId1,taskId2
```

格式为逗号分隔的字符串，例如 `taskId1,taskId2`。

### 通知推送（可选）

常见格式：

- Gotify: `gotify://host/token`
- Server 酱: `schan://SendKey`

多个通知地址可用逗号分隔。

## 🤔 常见问题

### Q：为什么任务时长一直为 0？

任务时长不是实时结算的。任务刚启动后，通常需要至少等待 **30 秒**，系统才会同步并更新时长。

如果等待后任务时长仍一直为 0，或出现「预估时长不为 0，但任务时长仍为 0」的情况，可能是账号存在风控限制。建议先停止工具，并手动打开目标直播间，观察任务时长是否会正常增加。

---

### Q：为什么预估时长和任务实际时长不一致？

可能是平台风控、直播/任务系统非实时结算或两者统计口径不同导致。

任务进度以任务接口返回为准；界面中的预估观看时长只能作为最大值参考，不保证等同于活动任务最终认可的有效时长。

---

### Q：为什么点击“自动获取”后没有拉起浏览器？

“自动获取”功能仅支持拉起典型安装路径下的 Chrome / Edge 浏览器。

如果浏览器安装在自定义路径，可能无法被自动识别或启动。

---

### Q：为什么首次使用“自动获取”会比较慢？

Selenium Manager 会自动下载缺失的浏览器驱动。

首次使用“自动获取”功能时，可能因下载驱动而耗时较久；若网络异常，也可能导致下载失败，进而无法启动浏览器。

---

### Q：线程数是不是越高越好？

**不是**。

线程数过高，或频繁开启 / 关闭任务，可能触发平台风控，导致请求受限或任务失败。

受个人网络环境差异及 B 站流控规则影响，加速效果存在软上限，建议根据实际情况调整线程数，推荐 **60~80** 线程。

---

### Q：风控了怎么办？

由于不同账号的账号状态和风控情况存在差异，建议先将账号静置 **30 分钟以上**，然后适当减少线程数后再重试。

如果仍无法正常增加任务时长，可优先使用官方直播间进行观看。

## 🚀 快速开始

### Windows

1. 安装 Python 3.10+
2. 克隆项目并安装依赖
   ```bash
   pip install -r requirements.txt
   ```
3. 启动 GUI
   ```bash
   python bilibili_gui.py
   ```

### Linux / macOS

1. 安装 Python 3.10+
2. 安装依赖
   ```bash
   pip install -r requirements.txt
   ```
3. 启动 CLI
   ```bash
   python bilibili.py --cookie "SESSDATA=xxx; bili_jct=xxx" --rooms "23612045"
   ```

## 📜 使用文档

### GUI（推荐）

```bash
python bilibili_gui.py
```

填入 Cookie、房间号、任务 ID 后点击“启动”。

你也可以在 GUI 中直接使用：

- Cookie 自动获取（浏览器登录后自动回填）
- 任务 ID 自动获取（抓取任务接口并自动回填）
- 配置文件保存/加载（JSON）

### CLI

- 获取命令帮助：`python bilibili.py --help`

```shell
Bilibili Drops Miner

Usage: python bilibili.py [OPTIONS]

Options:
   --cookie COOKIE                        		B站登录 Cookie
   --rooms ROOMS                          		房间号，逗号分隔
   --threads THREADS                      		每房间会话数（可加速任务进度）
   --reconnect-delay RECONNECT_DELAY      		断线重连延迟（秒）
   --disable-web-heartbeat                		关闭 x25Kn 业务心跳
   --task-ids TASK_IDS                    		用于进度监控的任务 ID
   --task-interval TASK_INTERVAL          		任务查询间隔（秒）
   --notify-urls NOTIFY_URLS              		通知 URL，逗号分隔
   --disable-task-notify                  		关闭任务完成通知	
   -h, --help                             		显示此帮助信息并退出
```

示例：

```bash
python bilibili.py \
   --cookie "SESSDATA=xxx; bili_jct=xxx" \
   --rooms "23612045,1017" \
   --threads 2 \
   --task-ids "taskId1,taskId2" \
   --notify-urls "gotify://host/token" \
   -v
```

## 📦 打包 EXE

```bash
python build.py               # 开发模式
python build.py --target gui  # PyInstaller 打包 GUI
python build.py --target cli  # PyInstaller 打包 CLI
python build_nuitka.py --target gui  # Nuitka 轻量打包 GUI
```

## 🧩 配置文件

GUI 支持保存/加载 JSON 配置文件，格式可参考 config.example.json。


## 🧑‍💻 开发

安装开发依赖：

```bash
pip install -r requirements.txt
```

本地调试：

- GUI: `python bilibili_gui.py`
- CLI: `python bilibili.py --help`

## ⭐ Stars

如果这个项目对你有帮助，欢迎点一个 Star。

## 📄 License

MIT

## 🥰 通过[爱发电](https://afdian.com/a/eriiiii)赞助
