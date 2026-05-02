#!/usr/bin/env python3
"""
newapi_checkin_camoufox.py — 基于 Camoufox + CDP + playwright-captcha 的 New API 签到脚本

架构:
  1. 纯 HTTP (requests/httpx) 快速尝试（无 Turnstile/PoW 的站点直接成功）
  2. CDP Chrome 获取 Turnstile token（最稳定的方式）
  3. Camoufox 浏览器解决 Cloudflare + 执行签到（兜底）
  4. PoW 求解（query 模式 + x-pow-token 模式）

用法:
  python3 newapi_checkin_camoufox.py --url <url> --session <s> --user-id <uid> --access-token <t>
  python3 newapi_checkin_camoufox.py --site-json '<json>'
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import urlparse

from curl_cffi import requests as cf_requests

# ── 路径设置 ──
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# ── 可选依赖 ──
try:
    from camoufox.async_api import AsyncCamoufox
    HAS_CAMOUFOX = True
except ImportError:
    HAS_CAMOUFOX = False

try:
    from playwright_captcha import ClickSolver, CaptchaType, FrameworkType
    HAS_CAPTCHA_SOLVER = True
except ImportError:
    HAS_CAPTCHA_SOLVER = False

try:
    from newapi_checkin import get_turnstile_token_via_cdp, CDP_PORT
    HAS_CDP = True
except ImportError:
    HAS_CDP = False
    CDP_PORT = 19825


# ═══════════════════════════════════════════
# 环境自检 & 自修复
# ═══════════════════════════════════════════

def ensure_ubo_addon():
    """确保 Camoufox 的 uBlock Origin addon 已正确安装"""
    addon_dir = os.path.expanduser("~/.cache/camoufox/addons/UBO")
    manifest = os.path.join(addon_dir, "manifest.json")
    if os.path.exists(manifest):
        return True
    print("🔧 uBlock Origin addon 缺失，正在安装...")
    os.makedirs(addon_dir, exist_ok=True)
    try:
        xpi_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
        xpi_path = os.path.join(addon_dir, "ublock.xpi")
        subprocess.run(["curl", "-sL", "-o", xpi_path, xpi_url], timeout=30, check=True)
        subprocess.run(["unzip", "-o", xpi_path, "-d", addon_dir], timeout=10,
                        capture_output=True, check=True)
        if os.path.exists(manifest):
            print("✅ uBlock Origin addon 安装成功")
            return True
    except Exception as e:
        print(f"⚠️ uBlock Origin addon 安装失败: {e}")
    return False


def ensure_chrome_running():
    """确保 CDP Chrome 正在运行，返回是否可用"""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=3)
        return True
    except Exception:
        pass

    # Chrome 没运行，尝试启动
    start_script = os.path.join(SCRIPT_DIR, "start-bb-browser.sh")
    if os.path.exists(start_script):
        print("🔄 Chrome 未运行，正在启动...")
        try:
            subprocess.run(["bash", start_script], capture_output=True, timeout=30)
            for i in range(15):
                time.sleep(1)
                try:
                    urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=3)
                    print(f"✅ Chrome 已就绪 ({i+1}s)")
                    return True
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ Chrome 启动失败: {e}")
    return False


def get_cdp_token_with_retry(site_url, site_key, session="", cookie_domain="", max_retries=2):
    """
    获取 CDP Turnstile token，失败时自动重启 Chrome 重试。

    已知问题: Chrome 长时间运行后 Turnstile widget 渲染可能失败（返回 <empty>）。
    解决方案: 失败后重启 Chrome 再重试。
    """
    if not HAS_CDP:
        return ""

    for attempt in range(max_retries):
        # 确保 Chrome 运行
        if not ensure_chrome_running():
            print(f"⚠️ Chrome 不可用 (attempt {attempt+1})")
            if attempt < max_retries - 1:
                print("🔄 重启 Chrome...")
                _restart_chrome()
            continue

        token = get_turnstile_token_via_cdp(
            site_url, site_key,
            session=session, cookie_domain=cookie_domain
        )
        if token:
            return token

        # token 为空 → Chrome 状态可能有问题，重启重试
        if attempt < max_retries - 1:
            print(f"⚠️ CDP Turnstile token 为空，重启 Chrome 重试 ({attempt+1}/{max_retries})...")
            _restart_chrome()

    return ""


def _restart_chrome():
    """强制重启 Chrome"""
    import urllib.request
    # 杀掉旧 Chrome
    try:
        subprocess.run(["pkill", "-f", f"remote-debugging-port={CDP_PORT}"],
                        capture_output=True, timeout=5)
        time.sleep(2)
    except Exception:
        pass

    # 找到 Chrome 二进制
    chrome_bin = ""
    for path in subprocess.run(
        ["find", "/root/.agent-browser/browsers", "-name", "chrome", "-type", "f"],
        capture_output=True, text=True, timeout=5
    ).stdout.strip().split("\n"):
        if path:
            chrome_bin = path
            break

    if not chrome_bin:
        print("⚠️ 找不到 Chrome 二进制")
        return

    # 启动 Chrome（确保有 --remote-allow-origins=*）
    env = {**os.environ, "DISPLAY": ":99"}
    subprocess.Popen(
        [chrome_bin,
         f"--remote-debugging-port={CDP_PORT}",
         "--remote-allow-origins=*",
         "--user-data-dir=/tmp/chrome-cdp-profile",
         "--no-first-run", "--no-default-browser-check",
         "--no-sandbox", "--disable-gpu", "--window-size=1280,720"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # 等待就绪
    for i in range(10):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=3)
            print(f"✅ Chrome 重启成功 ({i+1}s)")
            return
        except Exception:
            pass
    print("⚠️ Chrome 重启超时")


# ═══════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════

def normalize_url(url: str) -> str:
    return url.rstrip("/") + "/"


def build_checkin_url(base_url: str) -> str:
    return normalize_url(base_url) + "api/user/checkin"


def build_debug_url(base_url: str) -> str:
    return normalize_url(base_url) + "api/status"


def extract_message(body: dict) -> str:
    if not isinstance(body, dict):
        return str(body)
    return body.get("message", body.get("msg", body.get("error", str(body))))


def classify_result(status: int, body: dict, debug: dict = None) -> dict:
    msg = extract_message(body) if isinstance(body, dict) else str(body)

    if isinstance(body, dict):
        if body.get("success") is True:
            return {"status": "success", "message": msg}
        if body.get("ret") == 1 or body.get("code") == 0:
            return {"status": "success", "message": msg}
        # 优先检查 code 字段
        code = body.get("code", "")
        if isinstance(code, str) and code.startswith("pow_"):
            return {"status": "pow_required", "message": msg}
        if isinstance(code, str) and "turnstile" in code:
            return {"status": "turnstile_required", "message": msg}

    if "已签到" in msg or "已经签到" in msg:
        return {"status": "already", "message": msg}
    if "签到成功" in msg:
        return {"status": "success", "message": msg}
    if "turnstile" in msg.lower():
        return {"status": "turnstile_required", "message": msg}
    if "pow" in msg.lower():
        return {"status": "pow_required", "message": msg}
    if status == 401 or "未提供" in msg or "格式错误" in msg:
        return {"status": "auth_failed", "message": msg}
    if status == 403:
        return {"status": "cf_blocked", "message": msg}
    if "安全验证" in msg:
        return {"status": "pow_required", "message": msg}

    return {"status": "error", "message": msg, "http_status": status}


# ═══════════════════════════════════════════
# 纯 HTTP 快速签到（不需要浏览器）
# ═══════════════════════════════════════════

def try_checkin_via_http(base_url, session, user_id, access_token):
    """
    纯 HTTP 签到尝试。
    适用于不需要 Turnstile/PoW 的站点，或已缓存 cf_clearance 的站点。
    返回: (result_dict, need_turnstile, turnstile_site_key, need_pow, pow_mode)
    """
    normalized = normalize_url(base_url)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "New-API-User": str(user_id),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cache-Control": "no-store",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if session:
        headers["Cookie"] = f"session={session}"

    # 加载 cf_clearance
    cf_cache_dir = os.path.expanduser("~/.hermes/cf_cache")
    domain = urlparse(base_url).hostname
    cf_cache_file = os.path.join(cf_cache_dir, f"{domain}.json")
    if os.path.exists(cf_cache_file):
        try:
            with open(cf_cache_file) as f:
                cache = json.load(f)
                if time.time() - cache.get("updated", 0) < 86400:
                    cf = cache.get("cf_clearance", "")
                    if cf:
                        headers["Cookie"] = headers.get("Cookie", "") + f"; cf_clearance={cf}"
        except Exception:
            pass

    client = cf_requests.Session(impersonate='chrome', timeout=20)

    try:
        # 获取站点配置
        debug_resp = client.get(build_debug_url(base_url), headers=headers)
        need_turnstile = False
        turnstile_site_key = ""
        need_pow = False
        pow_mode = "replace"

        if debug_resp.status_code == 200:
            d = debug_resp.json().get("data", {})
            need_turnstile = d.get("turnstile_check", False)
            turnstile_site_key = d.get("turnstile_site_key", "")
            need_pow = d.get("pow_enabled", False)
            pow_mode = d.get("pow_mode", "replace")

        # 如果需要 Turnstile 或 PoW，不走 HTTP 路径
        if need_turnstile or need_pow:
            return None, need_turnstile, turnstile_site_key, need_pow, pow_mode

        # 直接签到
        resp = client.post(build_checkin_url(base_url), headers=headers, json={}, timeout=20)
        body = {}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        # ── 签名重试：部分站点要求 X-Checkin-Timestamp + X-Checkin-Signature ──
        if (
            isinstance(body, dict)
            and not body.get('success')
            and '签名' in str(body.get('message') or '')
            and str(user_id or '').strip()
        ):
            from datetime import datetime as _dt
            now_str = _dt.now().strftime('%Y-%m')
            nonce_url = f"{base_url.rstrip('/')}/api/user/checkin?month={now_str}"
            nonce_resp = client.get(nonce_url, headers=headers, timeout=20)
            nonce_body = {}
            try:
                nonce_body = nonce_resp.json()
            except Exception:
                pass
            nonce = (nonce_body.get('data') or {}).get('checkin_nonce', '')
            if nonce:
                ts = str(int(time.time()))
                sig_input = f'{user_id}:{ts}:{nonce}'
                signature = hashlib.sha256(sig_input.encode()).hexdigest()
                sign_headers = dict(headers)
                sign_headers['X-Checkin-Timestamp'] = ts
                sign_headers['X-Checkin-Signature'] = signature
                resp = client.post(build_checkin_url(base_url), headers=sign_headers, json={}, timeout=20)
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}

        result = classify_result(resp.status_code, body)
        result["http_status"] = resp.status_code
        return result, False, "", False, ""

    except Exception as e:
        return {"status": "error", "message": str(e)}, False, "", False, ""
    finally:
        client.close()


# ═══════════════════════════════════════════
# PoW (Proof of Work) 求解
# ═══════════════════════════════════════════

def solve_pow(challenge: str, difficulty: int, mode: str = "query") -> dict:
    if mode == "query":
        prefix = challenge
        target_bytes = difficulty // 8
        remaining_bits = difficulty % 8
        mask = (0xFF << (8 - remaining_bits)) & 0xFF if remaining_bits else 0

        for nonce_int in range(0x100000000):
            nonce_hex = format(nonce_int, '08x')
            h = hashlib.sha256((prefix + nonce_hex).encode()).digest()
            if h[:target_bytes] != b'\x00' * target_bytes:
                continue
            if remaining_bits and target_bytes < len(h):
                if (h[target_bytes] & mask) != 0:
                    continue
            return {"challenge": challenge, "nonce": nonce_hex, "hash": h.hex(), "mode": "query"}
    else:
        # x-pow-token 模式: difficulty = hex 前导零字符数
        # nonce 是整数直接拼接字符串（不是补零 hex）
        prefix_zeros = '0' * difficulty
        for nonce_int in range(0x100000000):
            digest = hashlib.sha256(f'{challenge}{nonce_int}'.encode()).hexdigest()
            if digest.startswith(prefix_zeros):
                return {"challenge": challenge, "nonce": nonce_int, "hash": digest, "mode": "x-pow-token"}
    return {}


def build_pow_header(pow_result: dict) -> str:
    payload = {
        "challenge": pow_result["challenge"],
        "pow": {"nonce": pow_result["nonce"], "hash": pow_result["hash"], "time": 0.0},
        "fingerprint": {"canvas": 0, "webgl": 0},
        "behavior": {"score": 100, "moves": 0, "dist": 0},
        "automation": [],
        "risk": 0,
        "ts": int(time.time() * 1000),
    }
    return base64.b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()


# ═══════════════════════════════════════════
# Camoufox 浏览器签到
# ═══════════════════════════════════════════

async def solve_cloudflare_challenge(page, timeout_ms: int = 30000) -> bool:
    if not HAS_CAPTCHA_SOLVER:
        return False
    try:
        has_cf = await page.evaluate("""() => {
            const hasIframe = !!document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            const title = (document.title || '').toLowerCase();
            return hasIframe || title.includes('just a moment') || title.includes('请稍候');
        }""")
        if not has_cf:
            return True

        print("🔒 检测到 Cloudflare 挑战...")
        for captcha_type in [CaptchaType.CLOUDFLARE_TURNSTILE, CaptchaType.CLOUDFLARE_INTERSTITIAL]:
            try:
                async with ClickSolver(framework=FrameworkType.CAMOUFOX, page=page) as solver:
                    await asyncio.wait_for(
                        solver.solve_captcha(captcha_container=page, captcha_type=captcha_type),
                        timeout=timeout_ms / 1000
                    )
                print(f"✅ {captcha_type.name} 已解决")
                await page.wait_for_timeout(2000)
                return True
            except (asyncio.TimeoutError, Exception) as e:
                print(f"⚠️ {captcha_type.name}: {e}")
        return False
    except Exception as e:
        print(f"⚠️ CF 挑战解决异常: {e}")
        return False


async def checkin_with_camoufox(
    base_url, session, user_id, access_token,
    turnstile_token="", cf_clearance="", headless=True
) -> dict:
    """
    使用 Camoufox 执行签到。
    turnstile_token 由外部（CDP）获取并传入。
    """
    normalized = normalize_url(base_url)
    result = {"status": "error", "message": "Unknown error"}

    if not HAS_CAMOUFOX:
        return {"status": "error", "message": "Camoufox 未安装"}

    # 准备 cookies
    cookies = []
    domain = urlparse(base_url).hostname
    if session:
        cookies.append({"name": "session", "value": session, "domain": domain, "path": "/"})
    if cf_clearance:
        cookies.append({"name": "cf_clearance", "value": cf_clearance, "domain": domain, "path": "/"})

    with tempfile.TemporaryDirectory(prefix="camoufox_") as tmp_dir:
        try:
            async with AsyncCamoufox(
                user_data_dir=tmp_dir, persistent_context=True,
                headless=headless, humanize=True, locale="zh-CN",
                disable_coop=True, config={"forceScopeAccess": True},
                i_know_what_im_doing=True,
            ) as browser:
                page = await browser.new_page()

                if cookies:
                    await browser.add_cookies(cookies)
                    print(f"🍪 已注入 {len(cookies)} 个 cookie")

                # 访问站点
                print(f"🌐 访问 {normalized}")
                await page.goto(normalized, wait_until="networkidle", timeout=30000)
                try:
                    await page.wait_for_function('document.readyState === "complete"', timeout=10000)
                except Exception:
                    await page.wait_for_timeout(3000)

                # 解决 CF 挑战
                await solve_cloudflare_challenge(page)

                # 获取站点配置
                debug_data = await page.evaluate(f"""async () => {{
                    try {{
                        const resp = await fetch('{build_debug_url(base_url)}', {{
                            credentials: 'include', headers: {{ 'Accept': 'application/json' }}
                        }});
                        return await resp.json();
                    }} catch (e) {{ return {{ error: String(e) }}; }}
                }}""")

                need_pow = False
                pow_mode = "replace"
                if debug_data and "data" in debug_data:
                    d = debug_data["data"]
                    need_pow = d.get("pow_enabled", False)
                    pow_mode = d.get("pow_mode", "replace")

                # PoW 求解（通过浏览器 fetch 获取 challenge）
                pow_payload = None
                if need_pow:
                    print("⚙️ 需要 PoW...")
                    pow_endpoints = [
                        f"{normalized}api/user/pow/challenge?action=checkin",
                        f"{normalized}api/pow/challenge",
                    ]
                    for pow_url in pow_endpoints:
                        pow_result = await page.evaluate(f"""async () => {{
                            try {{
                                const resp = await fetch('{pow_url}', {{
                                    credentials: 'include',
                                    headers: {{ 'Accept': 'application/json', 'Authorization': 'Bearer {access_token}', 'New-API-User': '{user_id}' }}
                                }});
                                const text = await resp.text();
                                let data; try {{ data = JSON.parse(text); }} catch {{ data = {{}}; }}
                                return {{ ok: resp.ok, data: data }};
                            }} catch (e) {{ return {{ ok: false }}; }}
                        }}""")
                        if pow_result and pow_result.get("ok") and pow_result.get("data", {}).get("data"):
                            pd = pow_result["data"]["data"]
                            cid = pd.get("challenge_id") or pd.get("challenge")
                            prefix = pd.get("prefix") or pd.get("challenge") or ""
                            diff = int(pd.get("difficulty") or 0)
                            mode = "query" if "prefix" in pd else "x-pow-token"
                            if cid and prefix and diff:
                                sol = solve_pow(prefix, diff, mode=mode)
                                if sol:
                                    sol["challenge_id"] = cid
                                    pow_payload = sol
                                    print(f"✅ PoW 已解决 (nonce={sol['nonce']})")
                                break

                # 构建签到 URL
                checkin_url = build_checkin_url(base_url)
                req_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "New-API-User": str(user_id),
                    "Cache-Control": "no-store",
                }

                query_params = []
                if turnstile_token:
                    query_params.append(f"turnstile={turnstile_token}")
                if pow_payload:
                    if pow_payload.get("mode") == "query":
                        query_params.append(f"pow_challenge={pow_payload.get('challenge_id', pow_payload['challenge'])}")
                        query_params.append(f"pow_nonce={pow_payload['nonce']}")
                    elif pow_payload.get("mode") == "x-pow-token":
                        req_headers["X-Pow-Token"] = build_pow_header(pow_payload)
                if query_params:
                    checkin_url += "?" + "&".join(query_params)

                # 执行签到
                print(f"📤 执行签到: {checkin_url[:100]}...")
                h_json = json.dumps(req_headers, ensure_ascii=False)
                checkin_result = await page.evaluate(f"""async () => {{
                    try {{
                        const resp = await fetch('{checkin_url}', {{
                            method: 'POST', credentials: 'include',
                            headers: {h_json}, body: JSON.stringify({{}})
                        }});
                        const text = await resp.text();
                        let data; try {{ data = JSON.parse(text); }} catch {{ data = {{ raw: text }}; }}
                        return {{ status: resp.status, data: data }};
                    }} catch (e) {{ return {{ status: 0, error: String(e) }}; }}
                }}""")

                if checkin_result.get("error"):
                    result = {"status": "error", "message": checkin_result["error"]}
                else:
                    result = classify_result(checkin_result.get("status", 0), checkin_result.get("data", {}))
                    result["http_status"] = checkin_result.get("status", 0)

                # 如果返回 pow_required，自动走 PoW 流程
                if result.get("status") == "pow_required":
                    print("⚙️ 服务端要求 PoW，正在获取挑战...")
                    pow_result = await page.evaluate("""async () => {
                        try {
                            const resp = await fetch('/api/pow/challenge', {credentials: 'include'});
                            return await resp.json();
                        } catch(e) { return {error: String(e)}; }
                    }""")
                    if pow_result.get('success') and pow_result.get('data'):
                        pd = pow_result['data']
                        challenge = pd.get('challenge', '')
                        difficulty = int(pd.get('difficulty', 0))
                        if challenge and difficulty:
                            print(f"⚙️ PoW: difficulty={difficulty}")
                            sol = solve_pow(challenge, difficulty, mode='x-pow-token')
                            if sol:
                                print(f"✅ PoW 已解决: nonce={sol['nonce']}")
                                # 构建 X-Pow-Token（需要 canvas/webgl fingerprint）
                                pow_token = {
                                    'challenge': challenge,
                                    'pow': {'nonce': sol['nonce'], 'hash': sol['hash'], 'time': 0},
                                    'fingerprint': {
                                        'canvas': await page.evaluate("() => { try { const e=document.createElement('canvas'); e.width=200; e.height=50; const t=e.getContext('2d'); t.textBaseline='top'; t.font='14px Arial'; t.fillStyle='#f60'; t.fillRect(50,0,80,30); t.fillStyle='#069'; t.fillText('POW-Shield',2,15); let h=0; const s=e.toDataURL(); for(let i=0;i<s.length;i++) h=(h<<5)-h+s.charCodeAt(i)|0; return h>>>0; } catch{return 0;} }"),
                                        'webgl': await page.evaluate("() => { try { const t=document.createElement('canvas').getContext('webgl'); if(!t) return 0; const n=t.getExtension('WEBGL_debug_renderer_info'); const s=n?t.getParameter(n.UNMASKED_RENDERER_WEBGL):''; let h=0; const str=[t.getParameter(t.MAX_TEXTURE_SIZE),s].join('|'); for(let j=0;j<str.length;j++) h=(h<<5)-h+str.charCodeAt(j)|0; return h>>>0; } catch{return 0;} }"),
                                    },
                                    'behavior': {'score': 100, 'moves': 10, 'dist': 500},
                                    'automation': [],
                                    'risk': 0,
                                    'ts': 0,
                                }
                                import base64 as _b64
                                x_pow_token = _b64.b64encode(json.dumps(pow_token).encode()).decode()
                                # 重新签到（用参数传递避免 JS 转义问题）
                                pow_headers = {
                                    'Content-Type': 'application/json', 'Accept': 'application/json',
                                    'Authorization': f'Bearer {access_token}', 'New-API-User': str(user_id),
                                    'Cache-Control': 'no-store', 'X-Pow-Token': x_pow_token,
                                }
                                pow_checkin_url = build_checkin_url(base_url)
                                pow_checkin_result = await page.evaluate(
                                    """async ([url, headers]) => {
                                        try {
                                            const resp = await fetch(url, {
                                                method: 'POST', credentials: 'include',
                                                headers: headers, body: JSON.stringify({})
                                            });
                                            const text = await resp.text();
                                            let data; try { data = JSON.parse(text); } catch { data = {raw: text}; }
                                            return { status: resp.status, data: data };
                                        } catch(e) { return { status: 0, error: String(e) }; }
                                    }""",
                                    [pow_checkin_url, pow_headers]
                                )
                                if not pow_checkin_result.get("error"):
                                    result = classify_result(pow_checkin_result.get("status", 0), pow_checkin_result.get("data", {}))
                                    result["http_status"] = pow_checkin_result.get("status", 0)
                                    result["mode"] = "camoufox+pow"

                # 缓存 cf_clearance
                try:
                    all_cookies = await browser.cookies()
                    for c in all_cookies:
                        if c.get("name") == "cf_clearance" and c.get("value"):
                            cache_dir = os.path.expanduser("~/.hermes/cf_cache")
                            os.makedirs(cache_dir, exist_ok=True)
                            with open(os.path.join(cache_dir, f"{domain}.json"), "w") as f:
                                json.dump({"cf_clearance": c["value"], "updated": time.time()}, f)
                            print(f"💾 已缓存 cf_clearance: {domain}")
                            break
                except Exception:
                    pass

        except Exception as e:
            result = {"status": "error", "message": f"Camoufox 异常: {e}"}

    return result


# ═══════════════════════════════════════════
# 统一签到入口
# ═══════════════════════════════════════════

def checkin(base_url, session, user_id, access_token, headless=True):
    """
    统一签到流程:
    1. HTTP 快速尝试（不需要 Turnstile/PoW 的站点直接成功）
    2. CDP 获取 Turnstile token + HTTP 签到
    3. Camoufox 浏览器签到（兜底）
    """
    # Step 1: HTTP 快速尝试
    print(f"\n{'='*50}")
    print(f"🔗 签到: {base_url}")
    print(f"{'='*50}")

    result, need_ts, ts_key, need_pow, pow_mode = try_checkin_via_http(
        base_url, session, user_id, access_token
    )

    if result:
        classification = result.get("status", "")
        if classification in ("success", "already"):
            print(f"✅ HTTP 直接签到: {result.get('message', '')}")
            return result
        if classification not in ("turnstile_required", "pow_required", "auth_failed", "security_check"):
            print(f"❌ HTTP 签到失败: {result.get('message', '')}")
            return result

    # Step 2: 需要 Turnstile → CDP 获取 token
    turnstile_token = ""
    if need_ts:
        domain = urlparse(base_url).hostname
        turnstile_token = get_cdp_token_with_retry(
            base_url, ts_key,
            session=session, cookie_domain=domain,
            max_retries=2
        )
        if turnstile_token:
            print(f"🔑 CDP Turnstile token: {turnstile_token[:30]}...")

            # 用 CDP token + HTTP 重新签到
            if not need_pow:
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "New-API-User": str(user_id),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                }
                if session:
                    headers["Cookie"] = f"session={session}"
                checkin_url = build_checkin_url(base_url) + f"?turnstile={turnstile_token}"
                try:
                    client = cf_requests.Session(impersonate='chrome', timeout=20)
                    resp = client.post(checkin_url, headers=headers, json={})
                    body = resp.json()

                    # ── 签名重试 ──
                    if (
                        isinstance(body, dict)
                        and not body.get('success')
                        and '签名' in str(body.get('message') or '')
                        and str(user_id or '').strip()
                    ):
                        from datetime import datetime as _dt2
                        now_str2 = _dt2.now().strftime('%Y-%m')
                        nonce_url2 = f"{base_url.rstrip('/')}/api/user/checkin?month={now_str2}"
                        try:
                            nonce_resp2 = client.get(nonce_url2, headers=headers, timeout=20)
                            nonce_body2 = nonce_resp2.json()
                            nonce2 = (nonce_body2.get('data') or {}).get('checkin_nonce', '')
                            if nonce2:
                                ts2 = str(int(time.time()))
                                sig2 = hashlib.sha256(f'{user_id}:{ts2}:{nonce2}'.encode()).hexdigest()
                                h2 = dict(headers)
                                h2['X-Checkin-Timestamp'] = ts2
                                h2['X-Checkin-Signature'] = sig2
                                resp = client.post(checkin_url, headers=h2, json={})
                                body = resp.json()
                        except Exception:
                            pass

                    client.close()
                    result = classify_result(resp.status_code, body)
                    result["http_status"] = resp.status_code
                    result["mode"] = "http+cdp"
                    if result.get("status") in ("success", "already"):
                        print(f"✅ CDP+HTTP 签到: {result.get('message', '')}")
                        return result
                except Exception as e:
                    print(f"⚠️ CDP+HTTP 签到失败: {e}")
        else:
            print("⚠️ CDP Turnstile token 获取失败")

    # Step 3: Camoufox 浏览器签到（兜底）
    if HAS_CAMOUFOX:
        print("🌐 使用 Camoufox 浏览器签到...")
        result = asyncio.run(checkin_with_camoufox(
            base_url=base_url, session=session, user_id=user_id,
            access_token=access_token, turnstile_token=turnstile_token,
            headless=headless,
        ))
        result["mode"] = "camoufox"
        return result

    return result or {"status": "error", "message": "无可用签到方式"}


# ═══════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Camoufox 版 New API 签到")
    parser.add_argument("--url", default="", help="站点 URL")
    parser.add_argument("--session", default="", help="session cookie")
    parser.add_argument("--user-id", default="", help="用户 ID")
    parser.add_argument("--access-token", default="", help="access token")
    parser.add_argument("--cf", default="", help="cf_clearance cookie")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--probe-only", action="store_true", help="仅探测状态")
    parser.add_argument("--site-json", default="", help="站点 JSON 配置")

    args = parser.parse_args()

    if args.site_json:
        try:
            site = json.loads(args.site_json)
            args.url = site.get("url", args.url)
            args.session = site.get("session", args.session)
            args.user_id = str(site.get("user_id", args.user_id))
            args.access_token = site.get("access_token", args.access_token)
            args.cf = site.get("cf", args.cf)
        except json.JSONDecodeError:
            print("❌ 无效的站点 JSON")
            sys.exit(1)

    if not args.url:
        parser.error("--url 或 --site-json 必须提供")
    if not args.user_id or not args.access_token:
        parser.error("--user-id 和 --access-token 必须提供")

    # 环境自检
    ensure_ubo_addon()

    if args.probe_only:
        from newapi_checkin_camoufox import probe_site_status  # noqa: avoid circular
        # 直接用 httpx 探测
        result, ts, ts_key, pow, pm = try_checkin_via_http(
            args.url, args.session, args.user_id, args.access_token
        )
        print(json.dumps({
            "turnstile": ts, "site_key": ts_key,
            "pow": pow, "pow_mode": pm,
            "http_result": result,
        }, ensure_ascii=False, indent=2))
        return

    headless = not args.no_headless
    result = checkin(
        args.url, args.session, args.user_id, args.access_token,
        headless=headless,
    )

    print(f"\n{'='*50}")
    print(f"📊 签到结果: {result.get('status', 'unknown')}")
    print(f"💬 消息: {result.get('message', '')}")
    if result.get("http_status"):
        print(f"🔗 HTTP: {result['http_status']}")
    if result.get("mode"):
        print(f"🔧 模式: {result['mode']}")
    print(f"{'='*50}")

    print("\n__RESULT_JSON__")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
