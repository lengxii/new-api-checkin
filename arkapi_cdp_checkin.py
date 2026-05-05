#!/usr/bin/env python3
"""
arkapi_cdp_checkin.py — 通过 CDP Chrome 执行 arkapi UI 签到（增强版）
用法: python3 arkapi_cdp_checkin.py --session <session> --access-token <token> --user-id <uid>

增强特性:
  - SPA 渲染失败自动重试（最多 3 次）
  - 按钮查找多策略 fallback
  - 签名检测自动重试 (game_integrity_missing_action)
"""
import argparse
import json
import time
import websocket
import requests
import sys

CDP_URL = "http://127.0.0.1:19825"
MAX_RENDER_RETRIES = 3
RENDER_POLL_MAX = 15


def get_ws_url():
    resp = requests.put(f"{CDP_URL}/json/new?about:blank", timeout=10)
    return resp.json()["webSocketDebuggerUrl"]


def cdp_send(ws, method, params=None, timeout=30):
    msg_id = int(time.time() * 1000) % 100000
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws.settimeout(min(5, deadline - time.time()))
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                return resp.get("result", {})
        except websocket.WebSocketTimeoutException:
            continue
    raise TimeoutError(f"CDP command {method} timed out")


def cdp_evaluate(ws, expression, timeout=15):
    result = cdp_send(ws, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    }, timeout=timeout)
    if "result" in result:
        if result["result"].get("type") == "undefined":
            return None
        return result["result"].get("value")
    return None


def wait_for_spa_render(ws) -> bool:
    """等待 SPA 渲染完成，返回是否成功"""
    for i in range(RENDER_POLL_MAX):
        time.sleep(2)
        body_len = cdp_evaluate(ws, "document.body?.innerText?.length || 0") or 0
        has_btn = cdp_evaluate(ws, """
            !!Array.from(document.querySelectorAll('button')).find(
                b => (b.textContent || '').trim().includes('签到') && !b.disabled
            )
        """)
        if body_len > 200 and has_btn:
            return True
    return False


def find_and_click_checkin_btn(ws) -> bool:
    """多策略查找并点击签到按钮"""
    strategies = [
        # 策略1: 精确匹配"立即签到"
        """
        Array.from(document.querySelectorAll('button')).find(
            b => (b.textContent || '').trim() === '立即签到' && !b.disabled
        )
        """,
        # 策略2: 包含"签到"
        """
        Array.from(document.querySelectorAll('button')).find(
            b => (b.textContent || '').trim().includes('签到') && !b.disabled
        )
        """,
        # 策略3: 任意包含"签到"的可点击元素
        """
        Array.from(document.querySelectorAll('[role="button"], a, span, div')).find(
            el => (el.textContent || '').trim().includes('签到') && el.offsetParent !== null
        )
        """,
    ]

    for i, strategy in enumerate(strategies):
        result = cdp_evaluate(ws, f"""
            (() => {{
                const el = ({strategy});
                if (el) {{ el.click(); return true; }}
                return false;
            }})()
        """)
        if result:
            return True
        time.sleep(0.5)

    return False


def handle_pow_captcha(ws) -> bool:
    """处理 PoW captcha，返回是否成功"""
    has_pow = cdp_evaluate(ws, "!!document.querySelector('.pow-captcha, .pow-icon')")
    if not has_pow:
        return True  # no PoW needed

    # 点击 PoW 图标
    cdp_evaluate(ws, """
        (() => {
            const el = document.querySelector('.pow-icon, .pow-captcha [role="button"]');
            if (el) el.click();
        })()
    """)

    # 等待 PoW 完成
    for i in range(30):
        time.sleep(1)
        state = cdp_evaluate(ws, """
            (() => {
                const glow = document.querySelector('.pow-glow');
                const label = document.querySelector('.pow-label');
                const modal = document.querySelector('.semi-modal-body');
                return {
                    width: glow?.style?.width || '',
                    label: label?.textContent?.trim() || '',
                    modalVisible: !!modal,
                };
            })()
        """) or {}

        if state.get("label") == "验证通过" or not state.get("modalVisible"):
            return True

    return False  # PoW timed out


def check_result(ws) -> dict:
    """检查签到结果"""
    final = cdp_evaluate(ws, "document.body?.innerText || ''") or ""

    # 检查 Toast
    toasts = cdp_evaluate(ws, """
        JSON.stringify(
            Array.from(document.querySelectorAll('.semi-toast-content-text, .semi-notification-notice-content'))
                .map(e => e.textContent)
        )
    """) or ""

    if "签到成功" in toasts or "签到成功" in final:
        # 提取获得金额
        import re
        amount_match = re.search(r'\$[\d.]+', toasts)
        amount = f" 获得 {amount_match.group()}" if amount_match else ""
        return {"status": "success", "message": f"签到成功{amount}", "mode": "cdp-ui"}

    if "今日已签到" in final:
        return {"status": "already", "message": "今日已签到"}

    # 检测完整性签名错误
    if "完整性" in final or "integrity" in final.lower():
        return {"status": "integrity_error", "message": "完整性验证失败，需要 UI 交互"}

    return {"status": "error", "message": f"签到结果未确认 (toasts={toasts[:200]})"}


def checkin(session, access_token, user_id, base_url="https://windhub.cc/"):
    domain = "windhub.cc"
    tab_id = None

    for attempt in range(MAX_RENDER_RETRIES):
        ws_url = None
        ws = None

        try:
            # Step 1: Create CDP tab & connect
            ws_url = get_ws_url()
            tab_id = ws_url.split("/")[-1]
            ws = websocket.create_connection(ws_url, timeout=30)

            # Step 2: Enable domains
            cdp_send(ws, "Network.enable")
            cdp_send(ws, "Page.enable")
            cdp_send(ws, "Runtime.enable")

            # Step 3: Inject session cookie
            cdp_send(ws, "Network.setCookie", {
                "name": "session", "value": session,
                "domain": domain, "path": "/", "secure": True,
            })

            # Step 4: Navigate to home, inject localStorage
            cdp_send(ws, "Page.navigate", {"url": base_url}, timeout=30)
            time.sleep(3)

            user_obj = json.dumps({
                "id": int(user_id), "username": "", "display_name": "",
                "role": 1, "status": 1, "token": access_token, "group": "default",
            })
            cdp_evaluate(ws, f"localStorage.setItem('user', '{user_obj}')")

            # Step 5: Navigate to personal page
            cdp_send(ws, "Page.navigate", {"url": base_url + "console/personal"}, timeout=30)

            # Step 6: Wait for SPA render
            if not wait_for_spa_render(ws):
                # 先检查当前 body 有没有用
                body = cdp_evaluate(ws, "document.body?.innerText || ''") or ""
                if "今日已签到" in body:
                    return {"status": "already", "message": "今日已签到"}

                ws.close()
                requests.put(f"{CDP_URL}/json/close/{tab_id}", timeout=5)
                continue  # retry

            # Step 7: Check if already done
            body = cdp_evaluate(ws, "document.body?.innerText || ''") or ""
            if "今日已签到" in body:
                return {"status": "already", "message": "今日已签到"}

            # Step 8: Click check-in button
            if not find_and_click_checkin_btn(ws):
                return {"status": "error", "message": "找不到签到按钮"}

            time.sleep(2)

            # Step 9: Handle PoW captcha
            handle_pow_captcha(ws)
            time.sleep(3)

            # Step 10: Check result
            result = check_result(ws)

            if result["status"] == "integrity_error" and attempt < MAX_RENDER_RETRIES - 1:
                # 完整性错误 — 关闭当前 tab，重试
                ws.close()
                requests.put(f"{CDP_URL}/json/close/{tab_id}", timeout=5)
                continue

            return result

        except Exception as exc:
            if attempt < MAX_RENDER_RETRIES - 1:
                time.sleep(2)  # brief pause before retry
                continue
            return {"status": "error", "message": str(exc)}

        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            if tab_id:
                try:
                    requests.put(f"{CDP_URL}/json/close/{tab_id}", timeout=5)
                except Exception:
                    pass

    return {"status": "error", "message": f"SPA 渲染失败 ({MAX_RENDER_RETRIES} 次重试后)"}


def main():
    parser = argparse.ArgumentParser(description="CDP Chrome arkapi UI 签到")
    parser.add_argument("--session", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--user-id", default="3084")
    parser.add_argument("--url", default="https://windhub.cc/")
    args = parser.parse_args()

    result = checkin(args.session, args.access_token, args.user_id, args.url)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["status"] in ("success", "already") else 1)


if __name__ == "__main__":
    main()