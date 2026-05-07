# new-api-checkin

New API / Ark API 多站点自动签到工具，支持 Telegram Bot 快捷命令与失败自动补签。

## 文件说明

| 文件 | 说明 |
|------|------|
| `api_checkin.py` | 多站点管理入口：添加/删除/列表/签到，Telegram 集成，站点缓存状态，多脚本 fallback 调度 |
| `newapi_checkin.py` | 原核心签到引擎（单站点签到、CDP、PoW、Turnstile） |
| `newapi_checkin_camoufox.py` | **新签到脚本**：Camoufox + CDP + PoW 全链路签到，定时 cron 直接调用此脚本 |
| `arkapi_cdp_checkin.py` | arkapi (windhub.cc) 专用 CDP Chrome UI 签到脚本，处理 SHIELD captcha / game_integrity_missing_action |

## 架构

```
                          ┌─────────────────────┐
                          │   Telegram / Cron    │
                          └─────────┬───────────┘
                                    │
                          ┌─────────▼───────────┐
                          │   api_checkin.py     │ ← 管理入口
                          │ add | del | list | qd│
                          └─────────┬───────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
          ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
          │newapi_checkin│ │newapi_checkin│ │arkapi_cdp    │
          │    .py       │ │ _camoufox.py │ │_checkin.py   │
          │  (旧脚本)     │ │  (新脚本 ⭐)  │ │(CDP UI 补签) │
          └──────────────┘ └──────┬───────┘ └──────▲───────┘
                                  │                 │
                                  │ 完整性失败时     │
                                  │ 自动调用 ───────┘
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │HTTP 签到  │ │CDP Turn- │ │Camoufox  │
              │(curl_cffi)│ │stile+HTTP│ │浏览器签到 │
              └──────────┘ └──────────┘ └──────────┘
```

## 签到流程（新脚本）

`newapi_checkin_camoufox.py` 是当前主要的签到脚本，cron 定时任务直接逐站调用它。

```
① HTTP 快速签到（curl_cffi，无浏览器）
    ├─ 成功 / 已签到 → 返回 ✓
    ├─ 需要 Turnstile → 进入 ②
    └─ 需要 PoW / 其他 → 进入 ③

② CDP Turnstile token 获取（Chrome CDP 端口 19825）
    ├─ 获取 token → HTTP 重试签到
    ├─ 成功 → 返回 ✓
    └─ 失败 → 进入 ③

③ Camoufox 浏览器签到
    ├─ 注入 session cookie，访问站点
    ├─ 解决 CF 挑战（playwright-captcha）
    ├─ PoW 求解（浏览器内 fetch challenge → Python 求解 → 重新提交）
    ├─ 成功 → 返回 ✓
    └─ game_integrity_missing_action → 进入 ④

④ CDP Chrome UI 自动补签 ⭐
    ├─ 检测 CDP Chrome 是否在运行
    │   ├─ 在运行 → 直接调用
    │   └─ 未运行 → 自动查找 Chrome 二进制并启动
    ├─ 启动后轮询等待 CDP 就绪（最多 15 秒）
    └─ 调用 arkapi_cdp_checkin.py 通过浏览器 UI 点击签到
```

### arkapi 自动补签机制（2026-05 新增）

arkapi (windhub.cc) 站点使用 SHIELD captcha 系统，即使 PoW 已正确求解，API 签到仍会返回：

```json
{"code": "game_integrity_missing_action", "message": "游戏动作缺少完整性标记，请刷新页面后重试"}
```

这意味着签到只能通过**真实浏览器 UI 操作**完成。

**自动补签流程**（在 `newapi_checkin_camoufox.py` 和 `api_checkin.py` 中均实现）：

1. Camoufox API 签到失败，识别 `game_integrity_missing_action` 或 `完整性`
2. 自动探测 CDP Chrome 是否运行（`curl http://127.0.0.1:19825/json/version`）
3. 如果 Chrome 不在运行：
   - 查找 Chrome 二进制（`find /root/.agent-browser/browsers -name chrome`）
   - 直接 `Popen` 启动（`--remote-debugging-port=19825 --remote-allow-origins=*`）
   - 轮询等待就绪（最多 15 秒）
4. 调用 `arkapi_cdp_checkin.py` 完成 UI 签到
5. 返回结果带 `mode: "cdp-ui"` 和 `message: "今日已签到 (CDP UI 补签)"`

**不需要用户手动发送"重新签到"消息**，整个链路在一条脚本内自动闭环。

### 各站点签到方式

| 站点 | 方式 | PoW | Turnstile |
|------|------|-----|-----------|
| elysiver | HTTP 直接 | 无 | 无 |
| arkapi (windhub.cc) | Camoufox → CDP UI 补签 | x-pow-token | 无 |
| runanytime | CDP Turnstile + PoW | query mode | ✅ |
| ciallo | CDP Turnstile + HTTP | 无 | ✅ |
| fengyeai | CDP Turnstile + HTTP | 无 | ✅ |
| dudu | HTTP 直接 | 无 | 无 |
| huaibao | HTTP + 签名头 | 无 | 无 |
| newcross | CDP Turnstile + Camoufox | 无 | ✅ |
| 67si | CDP Turnstile + HTTP | 无 | ✅ |
| 1394-ai | CDP Turnstile + HTTP | 无 | ✅ |

## 依赖

### Python 包

```bash
pip install curl_cffi camoufox playwright-captcha websocket-client httpx requests
```

### 虚拟环境

```bash
python3 -m venv ~/.venvs/scrapling
source ~/.venvs/scrapling/bin/activate
pip install curl_cffi camoufox playwright-captcha websocket-client httpx requests
```

### Camoufox 浏览器

```bash
~/.venvs/scrapling/bin/python -m camoufox fetch
```

### Chrome（CDP Turnstile 和 CDP UI 补签需要）

```bash
# 启动 CDP Chrome（端口 19825）
DISPLAY=:99 /path/to/chrome \
  --remote-debugging-port=19825 \
  --remote-allow-origins=* \
  --user-data-dir=/tmp/chrome-cdp-profile \
  --no-first-run --no-default-browser-check \
  --no-sandbox --disable-gpu \
  --window-size=1280,720 &
```

无桌面环境需先启动 Xvfb：

```bash
Xvfb :99 -screen 0 1280x720x24 &
```

**注意**：新脚本已内置 Chrome 自动拉起能力，正常情况下无需手动启动。但如果是首次部署，建议执行一次启动脚本确认 Chrome 路径和环境可用。

### 辅助脚本

| 文件 | 说明 |
|------|------|
| `start-bb-browser.sh` | 启动 Xvfb + Chrome + bb-browser daemon |
| `fix-camoufox-ubo.sh` | 修复 Camoufox uBlock Origin addon 缺失问题 |

## 环境配置

### 环境变量（可选）

```bash
# Patchright 浏览器路径
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/patchright-browsers"

# HTTP_PROXY 存在时 curl_cffi 需显式关闭代理
# 脚本内已通过 proxy={} 处理，无需额外设置
```

### 站点配置

配置文件：`~/.hermes/api_sites.json`

```json
{
  "sites": [
    {
      "name": "arkapi",
      "url": "https://windhub.cc/",
      "session": "session_cookie_value",
      "user_id": "3084",
      "access_token": "your_token"
    },
    {
      "name": "elysiver",
      "url": "https://elysiver.h-e.top/",
      "session": "session_cookie_value",
      "user_id": "6508",
      "access_token": "your_token"
    }
  ]
}
```

### 签到状态缓存

文件：`~/.hermes/api_checkin_status.json`

```json
{
  "sites": {
    "arkapi": {
      "date": "2026-05-08",
      "state": "already_checked_in",
      "message": "今日已签到 (CDP UI 补签)",
      "classification": "already_checked_in",
      "updated_at": "2026-05-08 04:42:00"
    }
  }
}
```

## 使用

### 直接签到（新脚本）

```bash
# JSON 配置方式（cron 定时任务使用）
~/.venvs/scrapling/bin/python newapi_checkin_camoufox.py \
  --site-json '{"url":"https://windhub.cc/","session":"xxx","user_id":"3084","access_token":"yyy"}'

# 参数方式
~/.venvs/scrapling/bin/python newapi_checkin_camoufox.py \
  --url https://elysiver.h-e.top/ \
  --session xxx \
  --user-id 6508 \
  --access-token yyy

# 仅探测状态（不签到）
~/.venvs/scrapling/bin/python newapi_checkin_camoufox.py \
  --site-json '...' --probe-only
```

### 批量管理（api_checkin.py）

```bash
# 签到全部站点
python3 api_checkin.py qd

# 签到指定站点
python3 api_checkin.py qd arkapi

# 站点列表（带签到状态）
python3 api_checkin.py list

# 添加站点
python3 api_checkin.py add 站点名 https://example.com/ session=xxx; user_id=1234; access_token=yyy

# 删除站点
python3 api_checkin.py del 站点名
```

### Telegram 快捷命令

| 命令 | 说明 |
|------|------|
| `api qd` | 全部站点签到 |
| `api qd arkapi` | 指定站点签到 |
| `api list` | 查看站点列表与签到状态 ✅/⬜ |
| `api add 站点名 URL session=xxx` | 添加站点 |
| `api del 站点名` | 删除站点 |

### Cron 定时任务

新脚本通过 Hermes Agent cron 调度，不依赖系统 crontab：

```
# 每日 00:10 运行 Camoufox 版签到脚本（10 站点）
# job id: 39899f0f1c2c
```

执行方式：逐站调用 `newapi_checkin_camoufox.py --site-json '...'`，arkapi 失败时自动触发 CDP UI 补签。

## CDP Chrome 自动管理

脚本内置了 CDP Chrome 生命周期管理：

- **探测**：每次需要 CDP 前通过 `curl http://127.0.0.1:19825/json/version` 检查可用性
- **自动拉起**：Chrome 不在运行时，查找 `/root/.agent-browser/browsers` 下的 chrome 二进制并 `Popen` 启动
- **就绪等待**：启动后轮询最多 15 秒直到 CDP 响应
- **不回退到 start-bb-browser.sh**：因为该脚本可能在 daemon 交互中卡住，新脚本直接管理 Chrome 进程

Chrome 可能因为内存压力被 OOM kill（宿主机 8GB + 4GB swap，swappiness=10），但签到前会自动重启，无需手动干预。

## 已知问题

### Chrome 长时间运行后 Turnstile 渲染失败

Chrome 长时间运行后 `window.turnstile.render()` 回调可能返回空 token。脚本对此有重试机制：失败后自动 kill 旧 Chrome + 启动新实例重试。

### Camoufox SPA 渲染不稳定

Camoufox 访问 New API SPA 页面时 body 可能间歇性为空（React hydration 时序问题）。arkapi 签到不依赖 Camoufox UI 路径——完整性验证优先走 CDP Chrome。

### curl_cffi + 代理冲突

HTTP_PROXY 环境变量存在时 curl_cffi 可能 TLS 超时。脚本通过 `proxy={}` 直接连接绕过本地代理。

## 需要的软件和工具

| 工具 | 用途 | 安装方式 |
|------|------|----------|
| Python 3.11+ | 运行脚本 | 系统自带 |
| curl_cffi | TLS 指纹伪装 HTTP 请求 | `pip install curl_cffi` |
| camoufox | 反检测 Firefox 浏览器 | `pip install camoufox` |
| playwright-captcha | CF/Turnstile 自动求解 | `pip install playwright-captcha` |
| websocket-client | CDP 通信 | `pip install websocket-client` |
| Chrome | Turnstile token + CDP UI 签到 | 手动下载或通过 agent-browser |
| Xvfb | 无头服务器虚拟显示 | `apt install xvfb` |

## 维护经验

### 签到脚本变更同步

当本地调试修改完成后，同步到 GitHub：

```bash
cd /root/new-api-checkin
cp /root/scripts/newapi_checkin_camoufox.py .
cp /root/scripts/api_checkin.py .
cp /root/scripts/arkapi_cdp_checkin.py .
git diff --stat
git add -A && git commit -m "描述" && git push origin main
```

### 状态缓存查看

```bash
cat ~/.hermes/api_checkin_status.json | python3 -m json.tool
```

### 检测签到是否成功

优先读取状态缓存，不要重新跑签到脚本：

```bash
python3 -c "
import json
cache=json.load(open('/root/.hermes/api_checkin_status.json'))
for name, s in cache.get('sites', {}).items():
    if s['date'] == '$(date +%F)':
        print(f'{name}: {s[\"state\"]} - {s[\"message\"]}')
"
```

## 相关项目

- [bb-browser](https://github.com/epiral/bb-browser) - 网站 adapter 平台
- [agent-browser-mcp](https://github.com/335234131/agent-browser-mcp) - TMWebDriver CDP 桥
- [Scrapling](https://github.com/niespodd/scrapling) - Cloudflare 绕过
- [Camoufox](https://github.com/daijro/camoufox) - 反检测 Firefox
- [playwright-captcha](https://github.com/AutomaApp/playwright-captcha) - 验证码自动求解

## License

MIT