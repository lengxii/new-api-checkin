#!/usr/bin/env python3
"""
arkapi_cdp_checkin.py — 通过 CDP Chrome 执行 arkapi UI 签到
用法: python3 arkapi_cdp_checkin.py --session <session> --access-token <token> --user-id <uid>
"""
import argparse
import json
import time
import websocket
import requests
import sys

CDP_URL = "http://127.0.0.1:19825"


def get_ws_url():
    """获取 Chrome 的 WebSocket 调试 URL"""
    resp = requests.put(f"{CDP_URL}/json/new?about:blank", timeout=10)
    return resp.json()["webSocketDebuggerUrl"]


def cdp_send(ws, method, params=None, timeout=30):
    """发送 CDP 命令并等待响应"""
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
    """执行 JavaScript 表达式并返回结果"""
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


def checkin(session, access_token, user_id, base_url="https://windhub.cc/"):
    """执行 arkapi UI 签到"""
    domain = "windhub.cc"

    # 创建新标签页
    ws_url = get_ws_url()
    ws = websocket.create_connection(ws_url, timeout=30)

    try:
        # 启用必要的域
        cdp_send(ws, "Network.enable")
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Runtime.enable")

        # 注入 session cookie
        cdp_send(ws, "Network.setCookie", {
            "name": "session",
            "value": session,
            "domain": domain,
            "path": "/",
            "secure": True,
        })

        # 注入 localStorage（通过先访问页面再设置）
        cdp_send(ws, "Page.navigate", {"url": base_url}, timeout=30)
        time.sleep(3)

        # 设置 localStorage
        user_obj = json.dumps({
            "id": int(user_id),
            "username": "",
            "display_name": "",
            "role": 1,
            "status": 1,
            "token": access_token,
            "group": "default",
        })
        cdp_evaluate(ws, f"localStorage.setItem('user', '{user_obj}')")

        # 导航到个人中心
        cdp_send(ws, "Page.navigate", {"url": base_url + "console/personal"}, timeout=30)

        # 等待 SPA 渲染
        for i in range(20):
            time.sleep(2)
            body_len = cdp_evaluate(ws, "document.body?.innerText?.length || 0") or 0
            if body_len > 200:
                break

        # 检查是否已签到
        body_text = cdp_evaluate(ws, "document.body?.innerText || ''") or ""
        if "今日已签到" in body_text:
            print("✅ 今日已签到")
            return {"status": "already", "message": "今日已签到"}

        if "登 录" in body_text:
            print("❌ 登录失败（session 无效或 localStorage 注入失败）")
            return {"status": "error", "message": "登录失败"}

        # 点击"立即签到"按钮
        clicked = cdp_evaluate(ws, """
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const btn = btns.find(b => b.textContent?.trim()?.includes('立即签到') && !b.disabled);
                if (btn) { btn.click(); return true; }
                // 备选：查找含"签到"的按钮
                const btn2 = btns.find(b => b.textContent?.trim()?.includes('签到') && !b.disabled);
                if (btn2) { btn2.click(); return true; }
                return false;
            })()
        """)
        if not clicked:
            print("❌ 找不到签到按钮")
            return {"status": "error", "message": "找不到签到按钮"}

        print("📤 点击了签到按钮")
        time.sleep(2)

        # 检查是否弹出 PoW captcha
        has_pow = cdp_evaluate(ws, "!!document.querySelector('.pow-captcha, .pow-icon')")
        if has_pow:
            print("🔐 检测到 PoW captcha，点击求解...")
            cdp_evaluate(ws, """
                (() => {
                    const el = document.querySelector('.pow-icon, .pow-captcha [role="button"]');
                    if (el) el.click();
                })()
            """)

            # 等待 PoW 求解完成（最多 30 秒）
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
                            modalVisible: !!modal
                        };
                    })()
                """) or {}
                if state.get("label") == "验证通过" or not state.get("modalVisible"):
                    print("✅ PoW 验证通过")
                    break

        time.sleep(3)

        # 检查最终结果
        final_text = cdp_evaluate(ws, "document.body?.innerText || ''") or ""
        if "今日已签到" in final_text or "签到成功" in final_text:
            print("✅ 签到成功")
            return {"status": "success", "message": "签到成功 (CDP Chrome)", "mode": "cdp-ui"}

        print("❌ 签到结果未确认")
        return {"status": "error", "message": "签到结果未确认"}

    finally:
        try:
            ws.close()
        except Exception:
            pass
        # 关闭标签页
        try:
            tag_id = ws_url.split("/")[-1]
            requests.put(f"{CDP_URL}/json/close/{tag_id}", timeout=5)
        except Exception:
            pass


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
