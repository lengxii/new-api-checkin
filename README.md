# new-api-checkin

New API / Ark API 多站点自动签到工具，支持 Telegram Bot 快捷命令。

## 功能

- 🔐 **requests 优先**：纯 HTTP 签到，无需浏览器
- 🌐 **Cloudflare 自动 fallback**：遇到 CF challenge 自动切换 Scrapling 浏览器
- 🧩 **PoW 安全验证**：自动解 PoW challenge（query 模式 + x-pow-token 模式）
- 🖥️ **CDP Turnstile 回退**：通过 Chrome CDP 协议自动获取 Turnstile token
- 🍪 **CDP Cookie 注入**：Turnstile 获取前自动注入 session cookie，确保登录态
- 🔄 **CDP 自动健康检查**：签到前检测 Chrome 是否可用，不可用自动重启
- 📦 **多站点批量签到**：一个命令签到所有站点
- 📊 **签到状态缓存**：本地记录签到结果，避免重复请求
- 🤖 **Telegram 快捷命令**：`api qd` / `api list` / `api add` / `api del`

## 文件说明

| 文件 | 说明 |
|------|------|
| `newapi_checkin.py` | 核心签到引擎（单站点签到、CDP、PoW、Turnstile） |
| `api_checkin.py` | 多站点管理 + Telegram Bot 集成（调用 newapi_checkin.py） |
| `requirements.txt` | Python 依赖 |

## 依赖

### Python 包

```bash
pip install requests scrapling websockets
```

### 虚拟环境（推荐）

```bash
python3 -m venv ~/.venvs/scrapling
source ~/.venvs/scrapling/bin/activate
pip install requests scrapling websockets
```

### Scrapling 浏览器（CF fallback 需要）

```bash
~/.venvs/scrapling/bin/python -m patchright install chromium
```

### Chrome + Xvfb（Turnstile token 获取）

```bash
# 安装 Xvfb
apt install xvfb

# 使用启动脚本（推荐，自动处理 Xvfb + Chrome + bb-browser）
bash start-bb-browser.sh

# 或手动启动
Xvfb :99 -screen 0 1280x720x24 &
DISPLAY=:99 google-chrome-stable \
  --remote-debugging-port=19825 \
  --no-sandbox \
  --disable-gpu \
  --window-size=1280,720 &
```

## 签到流程

```
┌──────────────────────────────────────────────────────────────────┐
│                         签到流程                                  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ① requests 直接请求签到 API                                     │
│     ├─ 成功 / 已签到 → 返回结果 ✓                                │
│     ├─ 需要 PoW → 自动求解 → 重试签到                            │
│     ├─ Turnstile token 为空 → 进入 ②                             │
│     └─ 其他错误 → 返回错误信息                                    │
│                                                                  │
│  ② CDP Turnstile token 获取                                      │
│     ├─ 检测 CDP Chrome 是否可用                                   │
│     │   ├─ 可用 → 继续                                           │
│     │   └─ 不可用 → 自动重启 Xvfb + Chrome → 等待就绪             │
│     ├─ 创建新 tab，注入 session cookie（确保登录态）               │
│     ├─ 导航到站点，注入 Turnstile 脚本                            │
│     ├─ 等待 token 生成（最多 45 秒）                              │
│     └─ 返回 token                                                │
│                                                                  │
│  ③ 重新签到（带 Turnstile token + PoW）                          │
│     ├─ 成功 → 返回结果 ✓                                         │
│     └─ Cloudflare 拦截 → 进入 ④                                  │
│                                                                  │
│  ④ Scrapling 浏览器 fallback                                     │
│     ├─ StealthyFetcher 加载页面（带 cookie）                      │
│     ├─ 页面内完成 Turnstile + PoW + 签到                         │
│     └─ 返回结果                                                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### CDP 自动健康检查

Chrome 进程可能意外挂掉（内存不足、OOM killed 等），导致所有需要 Turnstile 的站点签到失败。

`ensure_cdp_ready()` 在每次 CDP 操作前自动执行：

1. **探活**：`curl http://127.0.0.1:19825/json/version`
2. **不可用则自动重启**：执行 `start-bb-browser.sh`（启动 Xvfb + Chrome）
3. **等待就绪**：轮询最多 20 秒直到 CDP 响应
4. **继续签到**：无需人工干预

```
CDP: Chrome 不可用，正在自动重启...
CDP: Chrome 已就绪 (等待 2s)
```

## 使用

### 直接签到单个站点

```bash
# 使用虚拟环境
~/.venvs/scrapling/bin/python newapi_checkin.py \
  --url 'https://example.com/' \
  --session 'your_session_cookie' \
  --user-id 1234 \
  --access-token 'your_token'

# 或使用系统 Python
python3 newapi_checkin.py \
  --url 'https://example.com/' \
  --session 'your_session_cookie' \
  --user-id 1234 \
  --access-token 'your_token'
```

### 批量管理（Telegram Bot 集成）

```bash
# 添加站点
python3 api_checkin.py add 站点名 https://example.com/ session=xxx; user_id=1234; access_token=yyy

# 签到全部
python3 api_checkin.py qd

# 签到指定站点
python3 api_checkin.py qd 站点名

# 查看站点列表与状态
python3 api_checkin.py list

# 删除站点
python3 api_checkin.py del 站点名
```

### 站点配置

站点配置存储在 `~/.hermes/api_sites.json`，格式：

```json
{
  "sites": [
    {
      "name": "mysite",
      "url": "https://example.com/",
      "session": "session_cookie_value",
      "user_id": "1234",
      "access_token": "***"
    }
  ]
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `session` | ✅ | session cookie |
| `url` | ✅ | 站点 URL |
| `user_id` | 带 access_token 时必填 | 站点用户 ID |
| `access_token` | 可选 | Bearer token |
| `cf` | 可选 | cf_clearance cookie（CF 保护站点） |

## 需要的软件和工具

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| **Python 3.11+** | 运行脚本 | 系统自带 |
| **requests** | HTTP 请求 | `pip install requests` |
| **scrapling** | Cloudflare 绕过 | `pip install scrapling` |
| **websockets** | CDP 通信 | `pip install websockets` |
| **Chrome** | Turnstile token 获取 | `apt install google-chrome-stable` |
| **Xvfb** | 虚拟显示（无头服务器） | `apt install xvfb` |
| **patchright** | Scrapling 浏览器 | `python -m patchright install chromium` |

### 可选工具（增强功能）

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| **bb-browser** | 网站 adapter 平台 | `npm install -g bb-browser` |
| **agent-browser-mcp** | 物理输入/CDP 操作 | `pip install agent-browser-mcp` |
| **playwright** | 隔离浏览器自动化 | `pip install playwright` |

## 环境配置

### 1. 安装依赖

```bash
# 创建虚拟环境
python3 -m venv ~/.venvs/scrapling
source ~/.venvs/scrapling/bin/activate

# 安装 Python 包
pip install requests scrapling websockets

# 安装 Scrapling 浏览器
python -m patchright install chromium
```

### 2. 启动 Chrome 环境

```bash
# 安装 Xvfb
apt install xvfb

# 使用启动脚本（推荐）
bash start-bb-browser.sh

# 或手动启动
Xvfb :99 -screen 0 1280x720x24 &
DISPLAY=:99 google-chrome-stable \
  --remote-debugging-port=19825 \
  --no-sandbox \
  --disable-gpu \
  --window-size=1280,720 &
```

### 3. 验证环境

```bash
# 检查 Chrome CDP
curl -s http://127.0.0.1:19825/json/version

# 检查 Scrapling
~/.venvs/scrapling/bin/python -c "import scrapling; print(scrapling.__version__)"
```

### 4. 配置 Telegram Bot（可选）

在 Hermes Agent 的 `~/.hermes/config.yaml` 中配置 Telegram Bot，支持以下快捷命令：

| 命令 | 说明 |
|------|------|
| `api qd` | 全部站点签到 |
| `api qd 站点名` | 指定站点签到 |
| `api list` | 查看站点列表与签到状态 |
| `api add 站点名 URL session=xxx` | 添加站点 |
| `api del 站点名` | 删除站点 |

## 已知限制

- **Turnstile 在 headless 浏览器中无法初始化**：需要通过 CDP 协议连接桌面 Chrome 获取 token
- **CDP 回退需要 Chrome 运行**：签到前会自动检测并重启，但首次部署需手动启动一次
- **Xvfb 虚拟显示**：无头服务器需要 Xvfb 提供显示环境

## 相关项目

- [bb-browser](https://github.com/epiral/bb-browser) - 网站 adapter 平台
- [agent-browser-mcp](https://github.com/335234131/agent-browser-mcp) - TMWebDriver CDP 桥
- [Scrapling](https://github.com/niespodd/scrapling) - Cloudflare 绕过

## License

MIT
