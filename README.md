# new-api-checkin

New API / Ark API 多站点自动签到工具，支持 Telegram Bot 快捷命令。

## 功能

- 🔐 **requests 优先**：纯 HTTP 签到，无需浏览器
- 🌐 **Cloudflare 自动 fallback**：遇到 CF challenge 自动切换 Scrapling 浏览器
- 🧩 **PoW 安全验证**：自动解 PoW challenge（query 模式 + x-pow-token 模式）
- 📦 **多站点批量签到**：一个命令签到所有站点
- 📊 **签到状态缓存**：本地记录签到结果，避免重复请求
- 🤖 **Telegram 快捷命令**：`api qd` / `api list` / `api add` / `api del`

## 依赖

```bash
pip install requests scrapling
```

Scrapling 浏览器（仅 CF fallback 需要）：

```bash
python -m patchright install chromium
```

## 使用

### 直接签到单个站点

```bash
python newapi_checkin.py \
  --url 'https://example.com/' \
  --session 'your_session_cookie' \
  --user-id 1234 \
  --access-token 'your_token'
```

### 批量管理（Telegram Bot 集成）

```bash
# 添加站点
python api_checkin.py add 站点名 https://example.com/ session=xxx; user_id=1234; access_token=yyy

# 签到全部
python api_checkin.py qd

# 签到指定站点
python api_checkin.py qd 站点名

# 查看站点列表与状态
python api_checkin.py list

# 删除站点
python api_checkin.py del 站点名
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
      "access_token": "bearer_token"
    }
  ]
}
```

### 可选字段

| 字段 | 说明 |
|------|------|
| `session` | 必填，session cookie |
| `user_id` | 站点用户 ID（带 access_token 时必填） |
| `access_token` | Bearer token |
| `cf` | cf_clearance cookie（CF 保护站点可选） |

## 工作流程

```
请求签到 API
  ├─ 成功 / 已签到 → 直接返回
  ├─ 需要 PoW → 自动求解 → 重试
  ├─ Cloudflare challenge → Scrapling 浏览器 fallback
  └─ 其他错误 → 返回错误信息
```

## License

MIT
