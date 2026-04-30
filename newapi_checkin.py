#!/usr/bin/env python3
"""
通用 API 站点签到脚本
用法: python newapi_checkin.py --url 'https://example.com/' --session 'xxx' [--cf 'yyy'] [--user-id 6508] [--access-token token]
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from urllib.parse import urlencode, urlparse

import requests
from scrapling.fetchers import StealthyFetcher


CHECKIN_PATH = '/api/user/checkin'
REQUEST_TIMEOUT = 30
MAX_POW_NONCE = 1_000_000
CDP_PORT = 19825
CDP_TIMEOUT = 45


def normalize_url(url: str) -> str:
    raw = (url or '').strip()
    if not raw:
        raise ValueError('缺少站点 URL')
    if '://' not in raw:
        raw = f'https://{raw}'
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path).split('/')[0].split(':')[0].lstrip('.').lower()
    if not host:
        raise ValueError(f'无效站点 URL: {url}')
    path = parsed.path if parsed.netloc else ''
    path = path or '/'
    return f'{parsed.scheme or "https"}://{host}{path if path.startswith("/") else "/" + path}'


def build_cookie_domain(url: str) -> str:
    host = urlparse(url).netloc.split(':')[0].lstrip('.').lower()
    if not host:
        raise ValueError(f'无法从 URL 提取域名: {url}')
    return f'.{host}'


def build_checkin_url(base_url: str) -> str:
    normalized = normalize_url(base_url).rstrip('/')
    return f'{normalized}{CHECKIN_PATH}'


def requests_headers(user_id: str = '', access_token: str = '') -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Cache-Control': 'no-store',
        'Pragma': 'no-cache',
        'Origin': '',
        'Referer': '',
    }
    if user_id:
        headers['New-API-User'] = str(user_id)
    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'
    return headers


def create_requests_session(base_url: str, session: str, cf_clearance: str = '', user_id: str = '', access_token: str = '') -> requests.Session:
    normalized_url = normalize_url(base_url)
    host = urlparse(normalized_url).netloc.split(':')[0]
    sess = requests.Session()
    headers = requests_headers(user_id, access_token)
    headers['Origin'] = normalized_url.rstrip('/')
    headers['Referer'] = normalized_url
    sess.headers.update(headers)
    sess.cookies.set('session', session, domain=host, path='/')
    sess.cookies.set('session', session, path='/')
    if cf_clearance and cf_clearance.lower() not in ('placeholder', 'none', 'null', 'empty', ''):
        sess.cookies.set('cf_clearance', cf_clearance, domain=host, path='/')
        sess.cookies.set('cf_clearance', cf_clearance, path='/')
    return sess


def parse_json_response(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return None


def extract_message(body) -> str:
    if isinstance(body, dict):
        return str(body.get('message') or body.get('msg') or '')
    if isinstance(body, str):
        return body.strip()
    return ''


def classify_result(status: int, body, debug: dict | None = None) -> dict:
    debug = debug or {}
    message = extract_message(body)
    lowered = message.lower()
    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else ''
    lowered_body = body_text.lower()

    if 'just a moment' in lowered_body or 'enable javascript and cookies to continue' in lowered_body or "cType: 'managed'" in body_text or 'cf-challenge' in lowered_body or 'cdn-cgi/challenge-platform' in lowered_body:
        return {'kind': 'cloudflare_challenge', 'message': 'Cloudflare challenge'}

    if isinstance(body, dict) and body.get('success') is True:
        return {'kind': 'success', 'message': message or '签到成功'}
    if '已签到' in message:
        return {'kind': 'already_checked_in', 'message': message}
    if status in (401, 403) or '未登录' in message or '登录' in message or 'auth' in lowered or 'token' in lowered:
        return {'kind': 'auth_failed', 'message': message or f'认证失败（HTTP {status}）'}
    if 'pow' in lowered or debug.get('stage') in ('pow-challenge', 'pow-solve'):
        return {'kind': 'pow_required', 'message': message or '需要 PoW 验证'}
    if '安全验证' in message or 'captcha' in lowered:
        code = body.get('code') if isinstance(body, dict) else ''
        if code == 'pow_required' or 'pow' in lowered_body:
            return {'kind': 'pow_required', 'message': message or '需要 PoW 验证'}
        return {'kind': 'security_verification_required', 'message': message or '需要安全验证'}
    if 'turnstile' in lowered:
        return {'kind': 'security_verification_required', 'message': message or '需要安全验证'}
    if status == 0 and not message:
        return {'kind': 'empty_response', 'message': '空响应'}
    if status >= 400:
        return {'kind': 'request_failed', 'message': message or f'请求失败（HTTP {status}）'}
    if isinstance(body, str) and not message:
        return {'kind': 'empty_response', 'message': '空响应'}
    return {'kind': 'unknown', 'message': message or '未识别响应'}


def get_checkin_status(req_session: requests.Session, normalized_url: str) -> dict:
    base = normalized_url.rstrip('/')
    month_value = time.strftime('%Y-%m')
    candidates = [
        f"{base}{CHECKIN_PATH}?month={month_value}",
        f'{base}/api/user/self',
        f'{base}/api/user/status',
    ]
    for url in candidates:
        try:
            resp = req_session.get(url, timeout=REQUEST_TIMEOUT)
        except Exception as exc:
            return {'ok': False, 'stage': 'status-probe', 'message': str(exc), 'url': url}

        body = parse_json_response(resp)
        if not isinstance(body, dict):
            continue
        data = body.get('data') or {}
        if not resp.ok or not body.get('success'):
            continue

        if f"{CHECKIN_PATH}?month={month_value}" in url:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get('created_at'):
                        return {
                            'ok': True,
                            'url': url,
                            'already_checked_in': True,
                            'data': item,
                        }
            continue

        if url.endswith('/api/user/self'):
            continue

        indicators = {
            'checked_in': data.get('checked_in'),
            'checkin_today': data.get('checkin_today'),
            'is_checked_in': data.get('is_checked_in'),
            'has_checked_in_today': data.get('has_checked_in_today'),
        }
        for value in indicators.values():
            if isinstance(value, bool):
                return {
                    'ok': True,
                    'url': url,
                    'already_checked_in': value,
                    'data': data,
                }

        raw_text = json.dumps(data, ensure_ascii=False)
        if '已签到' in raw_text or 'checked in today' in raw_text.lower():
            return {
                'ok': True,
                'url': url,
                'already_checked_in': True,
                'data': data,
            }
    return {'ok': False, 'stage': 'status-probe', 'message': '未发现可用签到状态字段'}


def solve_pow_requests(base_url: str, req_session: requests.Session) -> dict:
    normalized_base = normalize_url(base_url).rstrip('/')
    challenge_candidates = [
        f'{normalized_base}/api/user/pow/challenge?action=checkin',
        f'{normalized_base}/api/pow/challenge',
    ]
    payload = None
    resp = None
    challenge_url = ''
    for candidate in challenge_candidates:
        resp = req_session.get(candidate, timeout=REQUEST_TIMEOUT)
        payload = parse_json_response(resp)
        if resp.ok and isinstance(payload, dict) and payload.get('data'):
            challenge_url = candidate
            break
    if resp is None or not resp.ok or not isinstance(payload, dict):
        return {'ok': False, 'stage': 'pow-challenge', 'status': getattr(resp, 'status_code', 0), 'payload': payload, 'text': getattr(resp, 'text', '')}

    data = payload.get('data') or {}
    challenge_id = data.get('challenge_id') or data.get('challenge')
    prefix = data.get('prefix') or data.get('challenge') or ''
    difficulty = int(data.get('difficulty') or 0)
    if not payload.get('success') or not challenge_id or not prefix:
        return {'ok': False, 'stage': 'pow-challenge', 'status': resp.status_code, 'payload': payload, 'text': resp.text, 'challenge_url': challenge_url}

    if '/api/pow/challenge' in challenge_url and 'prefix' not in data:
        nonce = 0
        prefix_zeroes = '0' * difficulty
        while nonce <= MAX_POW_NONCE:
            digest = hashlib.sha256(f'{prefix}{nonce}'.encode()).hexdigest()
            if digest.startswith(prefix_zeroes):
                pow_token_payload = {
                    'challenge': challenge_id,
                    'pow': {
                        'nonce': nonce,
                        'hash': digest,
                        'time': 0.0,
                    },
                    'fingerprint': {
                        'canvas': 0,
                        'webgl': 0,
                    },
                    'behavior': {
                        'score': 100,
                        'moves': 0,
                        'dist': 0,
                    },
                    'automation': [],
                    'risk': 0,
                    'ts': 0,
                }
                return {
                    'ok': True,
                    'challenge_id': challenge_id,
                    'nonce': nonce,
                    'difficulty': difficulty,
                    'prefix': prefix,
                    'mode': 'x-pow-token',
                    'challenge_url': challenge_url,
                    'pow_token_payload': pow_token_payload,
                }
            nonce += 1
        return {'ok': False, 'stage': 'pow-solve', 'message': 'Max attempts reached without solution', 'challenge_url': challenge_url}

    prefix_bytes = prefix.encode()
    full_bytes = difficulty // 8
    extra_bits = difficulty % 8
    mask = ((0xFF << (8 - extra_bits)) & 0xFF) if extra_bits else 0
    nonce = 0
    while nonce <= MAX_POW_NONCE:
        nonce_hex = f'{nonce:08x}'
        digest = hashlib.sha256(prefix_bytes + nonce_hex.encode()).digest()
        ok = all(b == 0 for b in digest[:full_bytes])
        if ok and extra_bits:
            ok = (digest[full_bytes] & mask) == 0
        if ok:
            return {
                'ok': True,
                'challenge_id': challenge_id,
                'nonce': nonce_hex,
                'difficulty': difficulty,
                'prefix': prefix,
                'mode': 'query',
                'challenge_url': challenge_url,
            }
        nonce += 1

    return {'ok': False, 'stage': 'pow-solve', 'message': 'Max attempts reached without solution', 'challenge_url': challenge_url}


def build_checkin_path(turnstile_token: str = '', pow_payload: dict | None = None) -> str:
    params = {}
    if turnstile_token:
        params['turnstile'] = turnstile_token
    if pow_payload and pow_payload.get('challenge_id'):
        params['pow_challenge'] = str(pow_payload['challenge_id'])
    if pow_payload and pow_payload.get('nonce'):
        params['pow_nonce'] = str(pow_payload['nonce'])
    query = urlencode(params)
    return f'{CHECKIN_PATH}?{query}' if query else CHECKIN_PATH


def build_checkin_request(normalized_url: str, pow_payload: dict | None = None) -> tuple[str, dict]:
    checkin_path = build_checkin_path('', pow_payload if pow_payload and pow_payload.get('mode') == 'query' else None)
    request_headers = {}
    if pow_payload and pow_payload.get('mode') == 'x-pow-token':
        pow_token_payload = dict(pow_payload.get('pow_token_payload') or {})
        pow_token_payload['ts'] = int(time.time() * 1000)
        request_headers['X-Pow-Token'] = base64.b64encode(json.dumps(pow_token_payload, separators=(',', ':')).encode()).decode()
    base = normalized_url.rstrip('/')
    checkin_url = f"{base}{checkin_path}"
    return checkin_url, request_headers


def checkin_via_requests(site_url: str, session: str, cf_clearance: str = '', user_id: str = '', access_token: str = '') -> dict:
    try:
        return _checkin_via_requests_impl(site_url, session, cf_clearance, user_id, access_token)
    except requests.exceptions.SSLError as exc:
        return {'status': 0, 'body': {'success': False, 'message': f'SSL 错误: {exc}'}, 'debug': {'mode': 'requests', 'stage': 'ssl-error', 'error': str(exc)}}
    except requests.exceptions.ConnectionError as exc:
        return {'status': 0, 'body': {'success': False, 'message': f'连接失败: {exc}'}, 'debug': {'mode': 'requests', 'stage': 'connection-error', 'error': str(exc)}}
    except requests.exceptions.Timeout:
        return {'status': 0, 'body': {'success': False, 'message': '请求超时'}, 'debug': {'mode': 'requests', 'stage': 'timeout'}}
    except Exception as exc:
        return {'status': 0, 'body': {'success': False, 'message': f'请求异常: {exc}'}, 'debug': {'mode': 'requests', 'stage': 'unknown-error', 'error': str(exc)}}


def _checkin_via_requests_impl(site_url: str, session: str, cf_clearance: str = '', user_id: str = '', access_token: str = '') -> dict:
    normalized_url = normalize_url(site_url)
    req_session = create_requests_session(normalized_url, session, cf_clearance, user_id, access_token)

    base = normalized_url.rstrip('/')
    status_resp = req_session.get(f'{base}/api/status', timeout=REQUEST_TIMEOUT)
    status_json = parse_json_response(status_resp) or {}
    status_data = status_json.get('data') if isinstance(status_json, dict) else {}
    turnstile_enabled = bool((status_data or {}).get('turnstile_check') or (status_data or {}).get('turnstileCheck') or (status_data or {}).get('turnstile_enabled'))
    turnstile_site_key = (status_data or {}).get('turnstile_site_key') or (status_data or {}).get('turnstileSiteKey') or ''
    pow_enabled = bool((status_data or {}).get('pow_enabled'))
    pow_mode = (status_data or {}).get('pow_mode') or 'replace'

    checkin_status = get_checkin_status(req_session, normalized_url)
    if checkin_status.get('ok') and checkin_status.get('already_checked_in') is True:
        return {
            'status': 200,
            'body': {
                'success': False,
                'message': '今日已签到（状态探测）',
                'data': checkin_status.get('data') or {},
            },
            'debug': {
                'mode': 'requests',
                'stage': 'status-probe',
                'powEnabled': pow_enabled,
                'powMode': pow_mode,
                'turnstileEnabled': turnstile_enabled,
                'turnstileSiteKey': turnstile_site_key,
                'statusProbe': checkin_status,
                'preventedRealCheckin': True,
            },
        }

    pow_payload = None
    if pow_enabled:
        pow_payload = solve_pow_requests(normalized_url, req_session)
        if not pow_payload.get('ok'):
            body = pow_payload.get('payload') or {'message': pow_payload.get('message') or 'PoW 计算失败', 'success': False}
            return {
                'status': pow_payload.get('status', 0),
                'body': body,
                'debug': {
                    'mode': 'requests',
                    'stage': pow_payload.get('stage'),
                    'powEnabled': pow_enabled,
                    'powMode': pow_mode,
                    'turnstileEnabled': turnstile_enabled,
                    'turnstileSiteKey': turnstile_site_key,
                    'challengeUrl': pow_payload.get('challenge_url'),
                    'statusProbe': checkin_status,
                },
            }

    checkin_url, request_headers = build_checkin_request(normalized_url, pow_payload)
    resp = req_session.post(checkin_url, headers=request_headers or None, timeout=REQUEST_TIMEOUT)
    body = parse_json_response(resp)
    if body is None:
        body = resp.text

    if (
        not pow_payload
        and str(user_id or '').strip()
        and access_token
        and isinstance(body, dict)
        and body.get('code') == 'pow_required'
    ):
        retry_pow_payload = solve_pow_requests(normalized_url, req_session)
        if not retry_pow_payload.get('ok'):
            retry_body = retry_pow_payload.get('payload') or {'message': retry_pow_payload.get('message') or 'PoW 计算失败', 'success': False}
            return {
                'status': retry_pow_payload.get('status', 0) or resp.status_code,
                'body': retry_body,
                'response_url': resp.url,
                'debug': {
                    'mode': 'requests',
                    'stage': retry_pow_payload.get('stage'),
                    'powEnabled': pow_enabled,
                    'powMode': pow_mode,
                    'turnstileEnabled': turnstile_enabled,
                    'turnstileSiteKey': turnstile_site_key,
                    'challengeUrl': retry_pow_payload.get('challenge_url'),
                    'statusProbe': checkin_status,
                    'implicitPowRetry': True,
                    'initialBody': body,
                },
            }

        retry_checkin_url, retry_headers = build_checkin_request(normalized_url, retry_pow_payload)
        retry_resp = req_session.post(retry_checkin_url, headers=retry_headers or None, timeout=REQUEST_TIMEOUT)
        retry_body = parse_json_response(retry_resp)
        if retry_body is None:
            retry_body = retry_resp.text
        return {
            'status': retry_resp.status_code,
            'body': retry_body,
            'response_url': retry_resp.url,
            'debug': {
                'mode': 'requests',
                'powEnabled': pow_enabled,
                'powMode': pow_mode,
                'turnstileEnabled': turnstile_enabled,
                'turnstileSiteKey': turnstile_site_key,
                'turnstileProvided': False,
                'powPayload': retry_pow_payload,
                'checkinPath': retry_checkin_url.removeprefix(normalized_url.rstrip('/')),
                'usedHeaders': sorted(retry_headers.keys()),
                'statusProbe': checkin_status,
                'implicitPowRetry': True,
                'initialBody': body,
            },
        }

    return {
        'status': resp.status_code,
        'body': body,
        'response_url': resp.url,
        'debug': {
            'mode': 'requests',
            'powEnabled': pow_enabled,
            'powMode': pow_mode,
            'turnstileEnabled': turnstile_enabled,
            'turnstileSiteKey': turnstile_site_key,
            'turnstileProvided': False,
            'powPayload': pow_payload,
            'checkinPath': checkin_url.removeprefix(normalized_url.rstrip('/')),
            'usedHeaders': sorted(request_headers.keys()),
            'statusProbe': checkin_status,
        },
    }


def get_turnstile_token_via_cdp(site_url: str, site_key: str) -> str:
    """通过 CDP 连接桌面 Chrome 获取 Turnstile token"""
    import asyncio

    try:
        import websockets
    except ImportError:
        return ''

    async def _get_token():
        import urllib.request as _urlreq

        # 检查 Chrome 是否运行
        try:
            _urlreq.urlopen(f'http://localhost:{CDP_PORT}/json/version', timeout=3)
            print('CDP: Chrome 已运行', flush=True)
        except Exception as e:
            print(f'CDP: Chrome 不可用（localhost:{CDP_PORT}）。请先启动 Chrome。', flush=True)
            return ''

        # 创建新 tab
        tab_id = ''
        ws_url = ''
        try:
            create_req = _urlreq.Request(
                f'http://localhost:{CDP_PORT}/json/new?about:blank',
                method='PUT',
            )
            create_resp = _urlreq.urlopen(create_req, timeout=5)
            new_tab = json.loads(create_resp.read())
            ws_url = new_tab.get('webSocketDebuggerUrl', '')
            tab_id = new_tab.get('id', '')
            if not ws_url:
                print('CDP: 创建 tab 失败（无 ws_url）', flush=True)
                return ''
            print('CDP: 创建新 tab 成功', flush=True)
        except Exception as exc:
            print(f'CDP: 创建 tab 失败: {exc}', flush=True)
            return ''

        def close_tab():
            try:
                _urlreq.urlopen(f'http://localhost:{CDP_PORT}/json/close/{tab_id}', timeout=3)
            except Exception:
                pass

        token_result = ''
        try:
            async with websockets.connect(ws_url, max_size=10*1024*1024, close_timeout=5) as ws:
                _msg_id = 0
                _pending = {}

                async def send_cmd(method, params=None):
                    nonlocal _msg_id
                    _msg_id += 1
                    mid = _msg_id
                    cmd = {'id': mid, 'method': method}
                    if params:
                        cmd['params'] = params
                    await ws.send(json.dumps(cmd))
                    # 等待对应 ID 的响应
                    while True:
                        resp = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(resp)
                        if data.get('id') == mid:
                            return data
                        # 丢弃事件通知等非 ID 响应

                # 启用 Page 事件
                await send_cmd('Page.enable')

                # 导航到站点
                await send_cmd('Page.navigate', {'url': site_url})

                # 等待页面加载
                await asyncio.sleep(6)

                # 检查 Turnstile 是否已加载
                check_result = await send_cmd('Runtime.evaluate', {
                    'expression': '!!window.turnstile',
                    'returnByValue': True,
                })
                has_ts = check_result.get('result', {}).get('result', {}).get('value', False)

                if not has_ts:
                    # 注入 Turnstile 脚本
                    inject_js = """
                    (function() {
                        if (window.turnstile) return 'already_loaded';
                        var s = document.createElement('script');
                        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                        s.async = true;
                        document.head.appendChild(s);
                        return 'injecting';
                    })()
                    """
                    inject_result = await send_cmd('Runtime.evaluate', {
                        'expression': inject_js,
                        'returnByValue': True,
                    })
                    print(f'CDP: Turnstile 注入: {inject_result.get("result",{}).get("result",{}).get("value")}', flush=True)

                    # 等待加载
                    for _ in range(15):
                        await asyncio.sleep(1)
                        r = await send_cmd('Runtime.evaluate', {
                            'expression': '!!window.turnstile',
                            'returnByValue': True,
                        })
                        if r.get('result', {}).get('result', {}).get('value'):
                            has_ts = True
                            break

                if not has_ts:
                    print('CDP: Turnstile 未加载', flush=True)
                    return ''

                print('CDP: Turnstile 已加载', flush=True)

                # 创建 widget 获取 token
                render_js = f"""
                (async function() {{
                    return await new Promise((resolve) => {{
                        const container = document.createElement('div');
                        container.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;';
                        document.body.appendChild(container);
                        let widgetId = null;
                        let settled = false;
                        const finish = (value) => {{
                            if (settled) return;
                            settled = true;
                            try {{ if (widgetId !== null) window.turnstile.remove(widgetId); }} catch (e) {{}}
                            container.remove();
                            resolve(value || '');
                        }};
                        try {{
                            widgetId = window.turnstile.render(container, {{
                                sitekey: '{site_key}',
                                callback: (token) => finish(token),
                                'error-callback': () => finish(''),
                                'expired-callback': () => finish(''),
                                'timeout-callback': () => finish(''),
                            }});
                        }} catch (e) {{
                            finish('');
                        }}
                        setTimeout(() => finish(''), 30000);
                    }});
                }})()
                """
                render_result = await send_cmd('Runtime.evaluate', {
                    'expression': render_js,
                    'awaitPromise': True,
                })
                token = render_result.get('result', {}).get('result', {}).get('value', '')
                print(f'CDP: 获取到 token: {repr(token[:40] if token else "<empty>")}', flush=True)
                token_result = token or ''

        except Exception as exc:
            print(f'CDP: 异常: {exc}', flush=True)
        finally:
            close_tab()

        return token_result

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(lambda: asyncio.run(_get_token())).result(timeout=CDP_TIMEOUT + 30)
    else:
        return asyncio.run(_get_token())


def checkin_via_browser(site_url: str, session: str, cf_clearance: str = '', user_id: str = '', access_token: str = '') -> dict:
    result = {}
    normalized_url = normalize_url(site_url)
    cookie_domain = build_cookie_domain(normalized_url)

    def on_response(resp):
        if CHECKIN_PATH in resp.url:
            try:
                result['body'] = resp.json()
            except Exception:
                result['body'] = resp.text
            result['status'] = resp.status
            result['response_url'] = resp.url

    def page_action(page):
        page.on('response', on_response)
        token_header = json.dumps(access_token) if access_token else 'null'
        user_header = json.dumps(str(user_id)) if user_id else 'null'
        script = f"""async () => {{
            const configuredUserId = {user_header};
            const configuredToken = {token_header};
            const status = (() => {{
                try {{
                    return JSON.parse(localStorage.getItem('status') || '{{}}');
                }} catch (error) {{
                    return {{}};
                }}
            }})();
            const loginUser = (() => {{
                try {{
                    return JSON.parse(localStorage.getItem('user') || 'null');
                }} catch (error) {{
                    return null;
                }}
            }})();
            const effectiveUserId = configuredUserId || (loginUser && loginUser.id ? String(loginUser.id) : null);
            const effectiveToken = configuredToken || (loginUser && loginUser.token ? String(loginUser.token) : null);
            const turnstileEnabled = Boolean(status.turnstile_check || status.turnstileCheck || status.turnstile_enabled);
            const turnstileSiteKey = status.turnstile_site_key || status.turnstileSiteKey || '';
            const powEnabled = Boolean(status.pow_enabled);
            const powMode = status.pow_mode || 'replace';

            const withAuthHeaders = (headers = {{}}) => {{
                const next = {{ ...headers, 'Cache-Control': 'no-store' }};
                if (effectiveUserId) next['New-API-User'] = effectiveUserId;
                if (effectiveToken) next['Authorization'] = `Bearer ${{effectiveToken}}`;
                return next;
            }};

            const buildCheckinPath = (turnstileToken = '', powPayload = null) => {{
                const params = new URLSearchParams();
                if (turnstileToken) params.set('turnstile', turnstileToken);
                if (powPayload?.challenge_id) params.set('pow_challenge', String(powPayload.challenge_id));
                if (powPayload?.nonce !== undefined && powPayload?.nonce !== null) params.set('pow_nonce', String(powPayload.nonce));
                const query = params.toString();
                return query ? `/api/user/checkin?${{query}}` : '/api/user/checkin';
            }};

            const requestJson = async (path, init = {{}}) => {{
                const resp = await fetch(path, {{
                    credentials: 'include',
                    ...init,
                    headers: withAuthHeaders(init.headers || {{}}),
                }});
                const text = await resp.text();
                let json = null;
                try {{
                    json = text ? JSON.parse(text) : null;
                }} catch (error) {{
                    json = null;
                }}
                return {{ resp, text, json }};
            }};

            const solvePow = async () => {{
                const challengeResp = await requestJson('/api/user/pow/challenge?action=checkin');
                const challengeJson = challengeResp.json || {{}};
                const challengeData = challengeJson.data || {{}};
                const challengeId = challengeData.challenge_id || challengeData.challenge;
                const prefix = challengeData.prefix || challengeData.challenge || '';
                const difficulty = Number(challengeData.difficulty || 0);
                if (!challengeResp.resp.ok || !challengeJson.success || !challengeId || !prefix) {{
                    return {{ ok: false, detail: challengeJson || challengeResp.text }};
                }}
                let nonce = 0;
                while (true) {{
                    const raw = `${{prefix}}${{nonce.toString(16).padStart(8, '0')}}`;
                    const buffer = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
                    const hashBytes = new Uint8Array(buffer);
                    const fullBytes = Math.floor(difficulty / 8);
                    const extraBits = difficulty % 8;
                    const prefixOk = Array.from(hashBytes.slice(0, fullBytes)).every((b) => b === 0);
                    const extraOk = !extraBits || (hashBytes[fullBytes] & ((0xff << (8 - extraBits)) & 0xff)) === 0;
                    if (prefixOk && extraOk) {{
                        return {{ ok: true, challenge_id: challengeId, nonce: nonce.toString(16).padStart(8, '0'), difficulty, prefix }};
                    }}
                    nonce += 1;
                }}
            }};

            const ensureTurnstileLoaded = async () => {{
                if (window.turnstile) {{
                    return true;
                }}
                if (!turnstileSiteKey) {{
                    return false;
                }}
                return await new Promise((resolve) => {{
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                    script.async = true;
                    script.onload = () => {{
                        const poll = (attempts) => {{
                            if (window.turnstile) {{
                                return resolve(true);
                            }}
                            if (attempts <= 0) {{
                                return resolve(false);
                            }}
                            setTimeout(() => poll(attempts - 1), 500);
                        }};
                        poll(20);
                    }};
                    script.onerror = (e) => {{
                        resolve(false);
                    }};
                    document.head.appendChild(script);
                    setTimeout(() => {{
                        resolve(!!window.turnstile);
                    }}, 15000);
                }});
            }};

            const getTurnstileToken = async () => {{
                if (!turnstileEnabled || !turnstileSiteKey) {{
                    return '';
                }}
                const loaded = await ensureTurnstileLoaded();
                if (!loaded || !window.turnstile) {{
                    return '';
                }}
                return await new Promise((resolve) => {{
                    const container = document.createElement('div');
                    container.style.cssText = 'position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;';
                    document.body.appendChild(container);
                    let widgetId = null;
                    let settled = false;
                    const finish = (value) => {{
                        if (settled) return;
                        settled = true;
                        try {{ if (widgetId !== null) window.turnstile.remove(widgetId); }} catch (error) {{}}
                        container.remove();
                        resolve(value || '');
                    }};
                    try {{
                        widgetId = window.turnstile.render(container, {{
                            sitekey: turnstileSiteKey,
                            callback: (tokenValue) => {{
                                finish(tokenValue);
                            }},
                            'error-callback': (e) => {{
                                finish('');
                            }},
                            'expired-callback': () => {{
                                finish('');
                            }},
                            'timeout-callback': () => {{
                                finish('');
                            }},
                        }});
                    }} catch (error) {{
                        finish('');
                    }}
                    setTimeout(() => {{
                        finish('');
                    }}, 30000);
                }});
            }};

            const shouldUsePow = () => {{
                if (!powEnabled) return false;
                if (powMode === 'replace' || powMode === 'supplement') return true;
                if (powMode === 'fallback') return !turnstileEnabled;
                return true;
            }};

            let powPayload = null;
            if (shouldUsePow()) {{
                powPayload = await solvePow();
                if (!powPayload.ok) {{
                    return {{
                        status: 0,
                        body: JSON.stringify(powPayload.detail || {{ message: 'PoW 计算失败', success: false }}),
                        debug: {{
                            status,
                            loginUser,
                            effectiveUserId,
                            hasEffectiveToken: Boolean(effectiveToken),
                            powPayload,
                            stage: 'pow-challenge',
                        }},
                    }};
                }}
            }}

            let turnstileToken = '';
            if (turnstileEnabled) {{
                turnstileToken = await getTurnstileToken();
            }}

            let response = await requestJson(buildCheckinPath(turnstileToken, powPayload), {{ method: 'POST' }});
            let message = response.json && typeof response.json.message === 'string' ? response.json.message : response.text;

            if (!turnstileToken && turnstileEnabled && typeof message === 'string' && message.includes('Turnstile')) {{
                turnstileToken = await getTurnstileToken();
                if (turnstileToken) {{
                    response = await requestJson(buildCheckinPath(turnstileToken, powPayload), {{ method: 'POST' }});
                    message = response.json && typeof response.json.message === 'string' ? response.json.message : response.text;
                }}
            }}

            if (powEnabled && typeof message === 'string' && /PoW|pow/.test(message) && (!powPayload || !powPayload.ok)) {{
                powPayload = await solvePow();
                if (powPayload.ok) {{
                    response = await requestJson(buildCheckinPath(turnstileToken, powPayload), {{ method: 'POST' }});
                    message = response.json && typeof response.json.message === 'string' ? response.json.message : response.text;
                }}
            }}

            return {{
                status: response.resp.status,
                body: response.text,
                debug: {{
                    status,
                    loginUser,
                    effectiveUserId,
                    hasEffectiveToken: Boolean(effectiveToken),
                    turnstileEnabled,
                    turnstileSiteKey,
                    turnstileProvided: Boolean(turnstileToken),
                    powEnabled,
                    powMode,
                    powPayload,
                    checkinPath: buildCheckinPath(turnstileToken, powPayload),
                }}
            }};
        }}"""
        fetch_result = page.evaluate(script)
        result.setdefault('fetch_result', fetch_result)
        result.setdefault('debug', fetch_result.get('debug') if isinstance(fetch_result, dict) else None)
        page.wait_for_timeout(3000)

    cookies = [
        {'name': 'session', 'value': session, 'domain': cookie_domain, 'path': '/'},
    ]
    if cf_clearance:
        cookies.append({'name': 'cf_clearance', 'value': cf_clearance, 'domain': cookie_domain, 'path': '/'})

    try:
        StealthyFetcher.fetch(
            normalized_url,
            headless=True,
            solve_cloudflare=True,
            cookies=cookies,
            load_dom=True,
            wait=5000,
            page_action=page_action,
        )
    except Exception as exc:
        return {
            'status': 0,
            'body': {'success': False, 'message': f'浏览器签到失败: {exc}'},
            'debug': {'mode': 'browser', 'stage': 'browser-error', 'error': str(exc)},
        }

    return result


def should_fallback_to_browser(result: dict) -> bool:
    body = result.get('body')
    debug = result.get('debug') or {}
    classification = result.get('classification') or classify_result(result.get('status', 0), body, debug)
    kind = classification.get('kind')

    if kind == 'cloudflare_challenge':
        return True

    if isinstance(body, dict):
        message = str(body.get('message', '') or '')
        turnstile_enabled = bool(debug.get('turnstileEnabled')) if isinstance(debug, dict) else False
        if turnstile_enabled and ('Turnstile' in message or 'turnstile' in message.lower()):
            return True

    return False


def checkin(site_url: str, session: str, cf_clearance: str = '', user_id: str = '', access_token: str = '') -> dict:
    requests_result = checkin_via_requests(site_url, session, cf_clearance, user_id, access_token)
    body = requests_result.get('body')
    classification = classify_result(requests_result.get('status', 0), body, requests_result.get('debug'))
    requests_result['classification'] = classification
    if isinstance(body, dict):
        message = str(body.get('message', '') or '')
        if body.get('success') or '已签到' in message:
            return requests_result
    elif isinstance(body, str) and ('签到成功' in body or '已签到' in body):
        return requests_result

    # 如果 Turnstile 启用但没有 token，尝试通过 CDP 获取
    debug = requests_result.get('debug') or {}
    turnstile_enabled = debug.get('turnstileEnabled', False)
    turnstile_provided = debug.get('turnstileProvided', False)
    turnstile_site_key = debug.get('turnstileSiteKey', '')

    if turnstile_enabled and not turnstile_provided and turnstile_site_key:
        print('尝试通过桌面 Chrome 获取 Turnstile token...')
        cdp_token = get_turnstile_token_via_cdp(site_url, turnstile_site_key)
        if cdp_token:
            print(f'CDP Turnstile token 获取成功: {cdp_token[:30]}...')
            # 使用 CDP token 重新签到
            normalized_url = normalize_url(site_url)
            req_session = create_requests_session(normalized_url, session, cf_clearance, user_id, access_token)
            
            # 检查是否需要 PoW
            pow_enabled = debug.get('powEnabled', False)
            pow_payload = None
            if pow_enabled:
                print('检测到 PoW 启用，正在获取 challenge...')
                pow_payload = solve_pow_requests(normalized_url, req_session)
                if not pow_payload.get('ok'):
                    print(f'PoW 获取失败: {pow_payload.get("message", "")}')
                    pow_payload = None
            
            checkin_path = build_checkin_path(cdp_token, pow_payload)
            checkin_url = f'{normalized_url.rstrip("/")}{checkin_path}'
            
            # 如果有 PoW，添加 X-Pow-Token header
            request_headers = {}
            if pow_payload and pow_payload.get('mode') == 'x-pow-token':
                import base64 as b64
                pow_token_payload = dict(pow_payload.get('pow_token_payload') or {})
                pow_token_payload['ts'] = int(time.time() * 1000)
                request_headers['X-Pow-Token'] = b64.b64encode(json.dumps(pow_token_payload, separators=(',', ':')).encode()).decode()
            
            try:
                resp = req_session.post(checkin_url, headers=request_headers or None, timeout=REQUEST_TIMEOUT)
                cdp_body = parse_json_response(resp)
                if cdp_body is None:
                    cdp_body = resp.text
                cdp_result = {
                    'status': resp.status_code,
                    'body': cdp_body,
                    'response_url': resp.url,
                    'debug': {
                        'mode': 'requests+cdp',
                        'turnstileEnabled': turnstile_enabled,
                        'turnstileProvided': True,
                        'turnstileSiteKey': turnstile_site_key,
                        'cdpToken': cdp_token[:30] + '...',
                        'powEnabled': pow_enabled,
                        'powPayload': pow_payload,
                    },
                }
                cdp_result['classification'] = classify_result(resp.status_code, cdp_body, cdp_result.get('debug'))
                if cdp_result['classification'].get('kind') != 'cloudflare_challenge':
                    return cdp_result
                print('CDP 签到仍被 Cloudflare 拦截，继续走浏览器兜底...')
            except Exception as exc:
                print(f'CDP 签到失败: {exc}')
        else:
            print('CDP Turnstile token 获取失败')

    if should_fallback_to_browser(requests_result):
        try:
            browser_result = checkin_via_browser(site_url, session, cf_clearance, user_id, access_token)
        except Exception as exc:
            browser_result = {
                'status': 0,
                'body': {'success': False, 'message': f'浏览器回退异常: {exc}'},
                'debug': {'mode': 'browser', 'stage': 'fallback-error', 'error': str(exc)},
            }
        if browser_result:
            browser_result.setdefault('debug', {})
            if isinstance(browser_result['debug'], dict):
                browser_result['debug'].setdefault('fallbackFromRequests', requests_result.get('debug'))
                browser_result['debug'].setdefault('fallbackReason', classification.get('kind'))
            browser_result['classification'] = classify_result(browser_result.get('status', 0), browser_result.get('body'), browser_result.get('debug'))
            return browser_result

    return requests_result


def main():
    parser = argparse.ArgumentParser(description='通用 API 站点签到')
    parser.add_argument('--url', '-U', required=True, help='站点 URL，例如 https://windhub.cc/')
    parser.add_argument('--session', '-s', required=True, help='session cookie')
    parser.add_argument('--cf', '-c', default='', help='可选 cf_clearance cookie')
    parser.add_argument('--user-id', '-u', default='', help='可选站点用户 ID（各站点可能不同）')
    parser.add_argument('--access-token', default='', help='可选 access token')
    args = parser.parse_args()

    print(f'站点: {normalize_url(args.url)}')
    print(f'用户 ID: {args.user_id or "(未提供)"}')
    print('开始签到...')

    result = checkin(args.url, args.session, args.cf, args.user_id, args.access_token)

    if not result:
        print('错误: 未能获取响应')
        sys.exit(1)

    if 'body' not in result and 'fetch_result' in result:
        fetch_result = result.get('fetch_result') or {}
        body_text = fetch_result.get('body', '')
        try:
            result['body'] = json.loads(body_text) if body_text else body_text
        except Exception:
            result['body'] = body_text
        result['status'] = fetch_result.get('status', result.get('status', '?'))
        if fetch_result.get('debug'):
            result['debug'] = fetch_result.get('debug')

    body = result.get('body', {})
    status = result.get('status', '?')
    classification = result.get('classification') or classify_result(int(status) if isinstance(status, int) else 0, body, result.get('debug'))

    if isinstance(body, dict):
        msg = body.get('message', '')
        success = body.get('success', False)
        print(f'状态码: {status}')
        print(f'结果: {msg}')
        print(f"分类: {classification.get('kind')}")
        data = body.get('data') or {}
        if 'quota_awarded' in data:
            print(f"奖励额度: {data['quota_awarded']}")
        if result.get('debug'):
            print(f"调试: {json.dumps(result['debug'], ensure_ascii=False, sort_keys=True)}")
        if success:
            print('签到成功！')
        elif '已签到' in msg:
            print('今日已签到')
        else:
            print('签到失败')
    else:
        print(f'响应: {body}')
        print(f"分类: {classification.get('kind')}")
        if result.get('debug'):
            print(f"调试: {json.dumps(result['debug'], ensure_ascii=False, sort_keys=True)}")

    sys.exit(0)


if __name__ == '__main__':
    main()
