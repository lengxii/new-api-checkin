#!/usr/bin/env python3
"""
API 签到站点管理 - Telegram 交互脚本
用法:
  /api_list                    列出所有站点与签到状态
  /api_add [站点名] URL        添加站点（发送 cookie 后 Bot 自动识别并保存）
  /api_del 站点名              删除站点
  /api_qd [站点名]             签到（可选指定站点）
  /api_qd_all                  全部站点签到

自动模式: 直接发送 cookie 字符串，Bot 自动解析 session/cf/access_token/user_id，识别域名，添加/更新站点
说明:
  - 新站点（携带 access_token）必须提供 user_id
  - cf_clearance 为可选项
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import re
import datetime
from pathlib import Path
from urllib.parse import urlparse

SITES_FILE = Path.home() / '.hermes' / 'api_sites.json'
STATUS_FILE = Path.home() / '.hermes' / 'api_checkin_status.json'
CHECKIN_SCRIPT = Path('/root/scripts/newapi_checkin.py')
CAMOUFOX_SCRIPT = Path('/root/scripts/newapi_checkin_camoufox.py')
CDP_ARKAPI_SCRIPT = Path('/root/scripts/arkapi_cdp_checkin.py')
VENV_PYTHON = Path.home() / '.venvs' / 'scrapling' / 'bin' / 'python'
FALLBACK_PYTHON = 'python3'
PATCHRIGHT_BROWSERS = Path.home() / '.cache' / 'patchright-browsers'


def _env_with_patchright() -> dict:
    """Return os.environ copy with PLAYWRIGHT_BROWSERS_PATH set to patchright cache."""
    env = os.environ.copy()
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(PATCHRIGHT_BROWSERS)
    return env


# Import shared normalize_url from newapi_checkin (lenient wrapper)
sys.path.insert(0, '/root/scripts')
import newapi_checkin as _ec
KNOWNS = KNOWN_DOMAINS = {
    'elysiver.h-e.top': 'elysiver',
    'windhub.cc': 'windhub',
    'ptlmon.com': 'ptlmon',
    'keungliang.dpdns.org': 'newcross',
}

COOKIE_FIELD_ALIASES = {
    'session': ('session',),
    'cf': ('cf_clearance', 'cf'),
    'access_token': ('access_token', 'token', 'access-token', 'x-access-token'),
    'user_id': ('user_id', 'userid', 'user-id', 'new-api-user', 'new-api-user-id'),
}


def load_sites():
    if not SITES_FILE.exists():
        return []
    try:
        data = json.loads(SITES_FILE.read_text())
    except Exception:
        return []

    sites = data.get('sites', []) if isinstance(data, dict) else []
    normalized = []
    for site in sites:
        if not isinstance(site, dict):
            continue
        normalized_site = normalize_site_entry(site)
        if normalized_site:
            normalized.append(normalized_site)
    return normalized


def load_status_cache():
    if not STATUS_FILE.exists():
        return {}
    try:
        data = json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_status_cache(cache: dict):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def get_cached_status(site_name: str) -> dict:
    cache = load_status_cache()
    site_cache = cache.get('sites', {}) if isinstance(cache.get('sites', {}), dict) else {}
    status = site_cache.get(site_name, {})
    return status if isinstance(status, dict) else {}


def update_cached_status(site_name: str, status: dict):
    cache = load_status_cache()
    if not isinstance(cache.get('sites'), dict):
        cache['sites'] = {}
    existing = cache['sites'].get(site_name, {})
    if not isinstance(existing, dict):
        existing = {}
    merged = dict(existing)
    merged.update(status)
    cache['sites'][site_name] = merged
    save_status_cache(cache)


def clear_cached_status(site_name: str):
    cache = load_status_cache()
    sites_cache = cache.get('sites', {}) if isinstance(cache.get('sites', {}), dict) else {}
    if site_name in sites_cache:
        sites_cache.pop(site_name, None)
        cache['sites'] = sites_cache
        save_status_cache(cache)


def cache_status_from_probe(site_name: str, status_info: dict):
    if status_info.get('source') == 'local-cache':
        return

    classification = status_info.get('classification')
    already_checked_in = status_info.get('already_checked_in')
    state = None
    if already_checked_in is True:
        state = 'already_checked_in'
    elif already_checked_in is False or classification == 'not_checked_in':
        state = 'not_checked_in'
    elif classification in {'security_verification_required', 'pow_required', 'cloudflare_challenge'}:
        state = 'verification_required'
    elif classification in {'auth_failed', 'request_failed', 'empty_response'}:
        state = 'error'
    elif classification == 'status_probe_failed':
        state = 'error'

    if not state:
        return

    today = datetime.date.today().isoformat()
    update_cached_status(site_name, {
        'date': today,
        'state': state,
        'message': str(status_info.get('message') or '').strip(),
        'classification': classification or '',
        'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


def detect_success_state(output: str) -> str:
    text = (output or '').strip()
    if not text:
        return ''
    if '今日已签到' in text or '已签到' in text:
        return 'already_checked_in'
    if '签到成功' in text or 'success' in text.lower():
        return 'success'
    return ''


def build_cached_status_for_list(site: dict) -> dict | None:
    cached = get_cached_status(site['name'])
    today = today_str()
    if not cached or cached.get('date') != today:
        return None

    state = cached.get('state')
    if state == 'already_checked_in':
        return {
            'ok': True,
            'already_checked_in': True,
            'classification': 'already_checked_in',
            'message': cached.get('message') or '今日已签到',
            'source': 'local-cache',
        }
    if state == 'success':
        return {
            'ok': True,
            'already_checked_in': True,
            'classification': 'success',
            'message': cached.get('message') or '签到成功',
            'source': 'local-cache',
        }
    if state == 'not_checked_in':
        return {
            'ok': True,
            'already_checked_in': False,
            'classification': 'not_checked_in',
            'message': cached.get('message') or '未签到',
            'source': 'local-cache',
        }
    return None


def today_str() -> str:
    return subprocess.run(
        ['date', '+%F'],
        capture_output=True,
        text=True,
        timeout=5,
        cwd='/root',
    ).stdout.strip()


def save_sites(sites):
    normalized = []
    seen_names = set()
    for site in sites:
        normalized_site = normalize_site_entry(site)
        if not normalized_site:
            continue
        name = normalized_site['name']
        if name in seen_names:
            continue
        seen_names.add(name)
        normalized.append(normalized_site)

    SITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITES_FILE.write_text(json.dumps({'sites': normalized}, ensure_ascii=False, indent=2))


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padded = payload + '=' * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        return json.loads(decoded)
    except Exception:
        return {}


def sanitize_host(value: str) -> str:
    raw = (value or '').strip().strip(';,')
    if not raw:
        return ''
    if '://' in raw:
        parsed = urlparse(raw)
        raw = parsed.netloc or parsed.path
    raw = raw.split('/')[0].split(':')[0].lstrip('.').lower()
    return raw


def extract_domain_from_session(session: str) -> str:
    payload = decode_jwt_payload(session)
    if not payload:
        return ''

    for field in ('jti', 'aud', 'domain', 'site', 'iss'):
        val = payload.get(field, '')
        if isinstance(val, str) and '.' in val:
            return sanitize_host(val)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and '.' in item:
                    return sanitize_host(item)
    return ''


def normalize_url(url: str) -> str:
    """Lenient wrapper: returns '' instead of raising ValueError."""
    try:
        return _ec.normalize_url(url)
    except (ValueError, Exception):
        return ''


def guess_site_name(url: str) -> str:
    parsed = urlparse(url)
    host = sanitize_host(parsed.netloc or parsed.path)
    for domain, name in KNOWN_DOMAINS.items():
        if host == domain or host.endswith('.' + domain) or domain.endswith('.' + host):
            return name
    parts = host.split('.')
    if len(parts) >= 2:
        return parts[-2]
    return host or 'site'


def extract_key_value_pairs(raw: str) -> dict:
    pairs = {}
    for match in re.finditer(r'([A-Za-z0-9_-]+)\s*=\s*([^;\s]+)', raw):
        key = match.group(1).lower()
        value = match.group(2).strip()
        if value:
            pairs[key] = value
    return pairs


def parse_cookie_string(raw: str) -> dict:
    values = {'session': '', 'cf': '', 'access_token': '', 'user_id': ''}
    pairs = extract_key_value_pairs(raw)
    for canonical, aliases in COOKIE_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in pairs:
                values[canonical] = pairs[alias]
                break
    return values


def parse_mixed_input(raw: str):
    url_match = re.search(r"https?://[^\s'\"]+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s'\"]*)?", raw)
    url = normalize_url(url_match.group(0)) if url_match else ''
    values = parse_cookie_string(raw)
    return url, values


def normalize_site_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        return {}

    url = normalize_url(entry.get('url', ''))
    if not url:
        return {}

    session = (entry.get('session') or '').strip()
    cf = (entry.get('cf') or entry.get('cf_clearance') or '').strip()
    if not session:
        return {}

    name = (entry.get('name') or '').strip() or guess_site_name(url)
    normalized = {
        'name': name,
        'url': url,
        'session': session,
    }
    if cf:
        normalized['cf'] = cf

    access_token = (entry.get('access_token') or '').strip()
    user_id = str(entry.get('user_id') or '').strip()
    if access_token and not user_id:
        return {}
    if access_token:
        normalized['access_token'] = access_token
    if user_id:
        normalized['user_id'] = user_id
    return normalized


def upsert_site(entry: dict):
    normalized_entry = normalize_site_entry(entry)
    if not normalized_entry:
        raise ValueError('站点配置不完整，至少需要有效的 URL、session；新站点如提供 access_token 则必须同时提供 user_id')

    sites = load_sites()
    idx = next((i for i, s in enumerate(sites) if s['name'] == normalized_entry['name']), -1)
    if idx >= 0:
        sites[idx].update(normalized_entry)
        normalized_entry = normalize_site_entry(sites[idx])
        sites[idx] = normalized_entry
        action = 'updated'
    else:
        sites.append(normalized_entry)
        action = 'added'

    save_sites(sites)
    return action, normalized_entry


def split_structured_command(raw: str) -> list[str]:
    raw = (raw or '').strip()
    if not raw:
        return []
    parts = raw.split(None, 2)
    if len(parts) < 2:
        return parts
    if parts[0] == 'api' and parts[1] in {'add', 'del', 'qd', 'list'}:
        action = parts[1]
        remainder = parts[2] if len(parts) > 2 else ''
        return [action, remainder]
    return parts


def run_status_probe(site: dict, python_cmd):
    probe_script = """
import json
import sys
sys.path.insert(0, '/root/scripts')
import newapi_checkin as ec

site = json.loads(sys.argv[1])
sess = ec.create_requests_session(
    site['url'],
    site['session'],
    site.get('cf', ''),
    site.get('user_id', ''),
    site.get('access_token', ''),
)
normalized = ec.normalize_url(site['url'])
result = ec.get_checkin_status(sess, normalized)

if result.get('ok') and result.get('already_checked_in') is False:
    result['classification'] = 'not_checked_in'
    result['source'] = 'status-probe'
elif not result.get('ok') or result.get('already_checked_in') is not True:
    if result.get('ok'):
        result.setdefault('already_checked_in', False)
        result.setdefault('classification', 'not_checked_in')
    else:
        result.setdefault('already_checked_in', None)
        result.setdefault('classification', 'status_probe_failed')
        result.setdefault('message', result.get('message') or '状态探测失败')
    result['source'] = 'status-probe'

print(json.dumps(result, ensure_ascii=False))
""".strip()
    command = [
        str(python_cmd),
        '-c',
        probe_script,
        json.dumps(site, ensure_ascii=False),
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        cwd='/root',
        env=_env_with_patchright(),
    )


def run_status_probe_with_fallback(site: dict):
    python_candidates = []
    if VENV_PYTHON.exists():
        python_candidates.append(str(VENV_PYTHON))
    python_candidates.append(FALLBACK_PYTHON)

    last_completed = None
    last_error = None
    for python_cmd in python_candidates:
        try:
            completed = run_status_probe(site, python_cmd)
        except FileNotFoundError as exc:
            last_error = exc
            continue

        combined_output = '\n'.join(part for part in [completed.stdout, completed.stderr] if part).lower()
        if completed.returncode != 0 and 'modulenotfounderror: no module named' in combined_output and python_cmd != FALLBACK_PYTHON:
            last_completed = completed
            continue
        return completed

    if last_completed is not None:
        return last_completed
    if last_error is not None:
        raise last_error
    raise FileNotFoundError('未找到可用的 Python 解释器')


def parse_status_probe_output(completed: subprocess.CompletedProcess) -> dict:
    text = (completed.stdout or completed.stderr or '').strip()
    if completed.returncode != 0:
        return {'ok': False, 'parse_error': text or f'probe exited {completed.returncode}'}
    try:
        data = json.loads(text) if text else {}
    except Exception:
        return {'ok': False, 'parse_error': text or 'invalid probe output'}
    return data if isinstance(data, dict) else {'ok': False, 'parse_error': text or 'invalid probe output'}


def format_status_icon(status_info: dict) -> str:
    classification = status_info.get('classification')
    if status_info.get('already_checked_in') is True:
        return '✅'
    if status_info.get('already_checked_in') is False:
        return '⬜'
    if classification in {'security_verification_required', 'pow_required', 'cloudflare_challenge'}:
        return '🔒'
    if classification == 'status_probe_failed':
        return '❔'
    if classification == 'not_checked_in':
        return '⬜'
    if classification in {'auth_failed', 'request_failed', 'empty_response'}:
        return '⚠️'
    if status_info.get('ok'):
        return '❔'
    return '⚠️'


def format_status_text(status_info: dict) -> str:
    classification = status_info.get('classification')
    message = str(status_info.get('message') or '').strip()
    if status_info.get('already_checked_in') is True:
        return '已签到'
    if status_info.get('already_checked_in') is False:
        return '未签到'
    if classification == 'not_checked_in':
        return '未签到'
    if classification == 'status_probe_failed':
        return f'探测失败({message})' if message else '探测失败'
    if classification == 'security_verification_required':
        return '需安全验证'
    if classification == 'pow_required':
        return '需 PoW 验证'
    if classification == 'cloudflare_challenge':
        return 'Cloudflare 校验'
    if classification == 'auth_failed':
        return '认证失效'
    if classification == 'request_failed':
        return '请求失败'
    if classification == 'empty_response':
        return '空响应'
    if status_info.get('parse_error'):
        return '状态探测失败'
    if message:
        return f'未知({message})'
    return '未知'


def cmd_list():
    sites = load_sites()
    if not sites:
        return '📭 暂无已配置站点'

    lines = [f'📋 站点列表: {len(sites)} 个', '图例: ✅ 已签到  ⬜ 未签到  🔒 需验证  ⚠️ 异常  ❔ 未知']
    for idx, site in enumerate(sites, 1):
        extras = []
        if site.get('user_id'):
            extras.append(f'user_id={site["user_id"]}')
        if site.get('access_token'):
            extras.append('access_token=已配置')
        extra_text = f' ({", ".join(extras)})' if extras else ''

        cached_status = build_cached_status_for_list(site)
        if cached_status is not None:
            status_info = cached_status
        else:
            try:
                completed = run_status_probe_with_fallback(site)
                status_info = parse_status_probe_output(completed)
                cache_status_from_probe(site['name'], status_info)
            except FileNotFoundError:
                status_info = {'ok': False, 'parse_error': 'Python 环境未找到'}
            except subprocess.TimeoutExpired:
                status_info = {'ok': False, 'parse_error': '状态探测超时'}
            except Exception as exc:
                status_info = {'ok': False, 'parse_error': str(exc)}

        icon = format_status_icon(status_info)
        status_text = format_status_text(status_info)
        source = ' [缓存]' if status_info.get('source') == 'local-cache' else ''
        lines.append(f'{idx}. {icon} [{site["name"]}] {status_text}{source}')
        lines.append(f'   URL: {site["url"]}{extra_text}')
    return '\n'.join(lines)


def cmd_add(raw: str):
    raw = raw.strip()
    if not raw:
        return (
            '📌 添加站点方式:\n\n'
            '1️⃣ 完整格式:\n'
            '/api_add 站点名 URL session=xxx; cf_clearance=yyy; access_token=zzz; user_id=123\n\n'
            '2️⃣ 粘贴 cookie 自动识别:\n'
            '直接发送: session=xxx; cf_clearance=yyy\n'
            '如 JWT 可解析域名，Bot 会自动补全站点 URL 与站点名\n\n'
            '3️⃣ 带 URL + cookie:\n'
            '/api_add https://windhub.cc/ session=xxx; cf_clearance=yyy'
        )

    auto_url, parsed_values = parse_mixed_input(raw)
    session = parsed_values['session']
    cf = parsed_values['cf']
    access_token = parsed_values['access_token']
    user_id = parsed_values['user_id']

    tokens = raw.split()
    first_token = tokens[0] if tokens else ''
    first_looks_like_kv = '=' in first_token
    first_looks_like_url = first_token.startswith(('http://', 'https://')) or ('.' in first_token and '/' in first_token)

    if session and not first_looks_like_url and not first_looks_like_kv:
        parts = raw.split(maxsplit=2)
        if len(parts) >= 2 and (parts[1].startswith(('http://', 'https://')) or '.' in parts[1]):
            name = parts[0].strip()
            url = normalize_url(parts[1])
        else:
            name = ''
            url = auto_url
    elif session and not re.search(r'https?://', raw) and raw.lstrip().startswith('session='):
        name = ''
        url = auto_url
    else:
        parts = raw.split()
        if len(parts) < 2:
            return (
                f'❌ 参数不足。需要至少 2 个参数 (URL session)，得到 {len(parts)} 个\n'
                '示例: /api_add https://windhub.cc/ session=xxx; cf_clearance=yyy\n'
                '或直接发送: session=xxx; cf_clearance=yyy'
            )
        first = parts[0]
        if first.startswith(('http://', 'https://')) or ('.' in first and '=' not in first):
            name = ''
            url = normalize_url(first)
            parsed_values = parse_cookie_string(' '.join(parts[1:]))
        else:
            if len(parts) < 3:
                return (
                    f'❌ 参数不足。需要至少 3 个参数 (站点名 URL session)，得到 {len(parts)} 个\n'
                    '示例: /api_add windhub https://windhub.cc/ session=xxx; cf_clearance=yyy'
                )
            name = first.strip()
            url = normalize_url(parts[1])
            parsed_values = parse_cookie_string(' '.join(parts[2:]))

        session = parsed_values['session']
        cf = parsed_values['cf']
        access_token = parsed_values['access_token']
        user_id = parsed_values['user_id']

    if not url and session:
        domain = extract_domain_from_session(session)
        if domain:
            url = normalize_url(domain)

    if not url:
        return '❌ 无法确定站点 URL，请使用 /api_add 站点名 URL session=xxx[; cf_clearance=yyy][; access_token=zzz; user_id=123]'
    if not session:
        return '❌ 无法解析 session，请检查格式'
    if access_token and not user_id:
        return '❌ 新站点如提供 access_token，必须同时提供 user_id'

    entry = {
        'name': name or guess_site_name(url),
        'url': url,
        'session': session,
    }
    if cf:
        entry['cf'] = cf
    if access_token:
        entry['access_token'] = access_token
    if user_id:
        entry['user_id'] = user_id

    try:
        action, saved = upsert_site(entry)
    except ValueError as exc:
        return f'❌ {exc}'

    msg = '♻️ 已更新站点' if action == 'updated' else '✅ 已添加站点'
    extras = []
    if saved.get('user_id'):
        extras.append(f'user_id={saved["user_id"]}')
    if saved.get('access_token'):
        extras.append('access_token=已配置')
    extra_text = f'\n附加信息: {", ".join(extras)}' if extras else ''
    return f'{msg}: [{saved["name"]}]\nURL: {saved["url"]}\n站点名: {saved["name"]}{extra_text}'


def cmd_del(raw: str):
    name = raw.strip()
    if not name:
        return '❌ 用法: /api_del 站点名'
    sites = load_sites()
    new_sites = [s for s in sites if s['name'] != name]
    if len(new_sites) == len(sites):
        return f'❌ 未找到站点: [{name}]'
    save_sites(new_sites)
    clear_cached_status(name)
    return f'🗑️ 已删除站点: [{name}]'


def run_checkin(site: dict, python_cmd, *, user_id_override=None):
    command = [
        str(python_cmd),
        str(CHECKIN_SCRIPT),
        '--url',
        site['url'],
        '--session',
        site['session'],
    ]
    cf_value = site.get('cf', '')
    if cf_value:
        command.extend(['--cf', cf_value])
    user_id = user_id_override if user_id_override is not None else site.get('user_id')
    if user_id:
        command.extend(['--user-id', str(user_id)])
    if site.get('access_token'):
        command.extend(['--access-token', site['access_token']])

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        cwd='/root',
        env=_env_with_patchright(),
    )


def run_checkin_with_fallback(site: dict, *, user_id_override=None):
    python_candidates = []
    if VENV_PYTHON.exists():
        python_candidates.append(str(VENV_PYTHON))
    python_candidates.append(FALLBACK_PYTHON)

    last_completed = None
    last_error = None
    for python_cmd in python_candidates:
        try:
            completed = run_checkin(site, python_cmd, user_id_override=user_id_override)
        except FileNotFoundError as exc:
            last_error = exc
            continue

        combined_output = '\n'.join(part for part in [completed.stdout, completed.stderr] if part).lower()
        if completed.returncode != 0 and 'modulenotfounderror: no module named' in combined_output and python_cmd != FALLBACK_PYTHON:
            last_completed = completed
            continue

        # 检测 game_integrity_missing_action（arkapi 等需要 UI 签到的站点）
        # 自动用 CDP Chrome 脚本重试（比 Camoufox 更稳定）
        if ('完整性' in combined_output or 'integrity' in combined_output):
            # 优先用 CDP arkapi 脚本
            if CDP_ARKAPI_SCRIPT.exists():
                print(f'⚠️ 检测到完整性验证要求，改用 CDP Chrome 签到...')
                for cdp_python in python_candidates:
                    try:
                        completed2 = run_checkin_cdp_arkapi(site, cdp_python, user_id_override=user_id_override)
                        return completed2
                    except FileNotFoundError:
                        continue
                    except Exception:
                        continue
            # 备选用 Camoufox 脚本
            if CAMOUFOX_SCRIPT.exists():
                print(f'⚠️ CDP 不可用，改用 Camoufox 脚本重试...')
                for camoufox_python in python_candidates:
                    try:
                        completed2 = run_checkin_camoufox(site, camoufox_python, user_id_override=user_id_override)
                        return completed2
                    except FileNotFoundError:
                        continue
                    except Exception:
                        continue

        return completed

    if last_completed is not None:
        return last_completed
    if last_error is not None:
        raise last_error
    raise FileNotFoundError('未找到可用的 Python 解释器')


def run_checkin_camoufox(site: dict, python_cmd, *, user_id_override=None):
    """使用 Camoufox 脚本执行签到（支持 UI 签到）"""
    command = [
        'xvfb-run', '-a',
        str(python_cmd),
        str(CAMOUFOX_SCRIPT),
        '--url',
        site['url'],
        '--session',
        site['session'],
    ]
    cf_value = site.get('cf', '')
    if cf_value:
        command.extend(['--cf', cf_value])
    user_id = user_id_override if user_id_override is not None else site.get('user_id')
    if user_id:
        command.extend(['--user-id', str(user_id)])
    if site.get('access_token'):
        command.extend(['--access-token', site['access_token']])

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        cwd='/root',
        env=_env_with_patchright(),
    )


def run_checkin_cdp_arkapi(site: dict, python_cmd, *, user_id_override=None):
    """使用 CDP Chrome 执行 arkapi UI 签到"""
    user_id = user_id_override if user_id_override is not None else site.get('user_id')
    command = [
        str(python_cmd),
        str(CDP_ARKAPI_SCRIPT),
        '--session', site['session'],
        '--access-token', site.get('access_token', ''),
        '--user-id', str(user_id or '3084'),
        '--url', site['url'],
    ]

    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        cwd='/root',
        env=_env_with_patchright(),
    )


def is_checkin_success(completed: subprocess.CompletedProcess, output: str) -> bool:
    text = (output or '').strip()
    if completed.returncode != 0:
        return False
    lowered = text.lower()
    success_keywords = ['签到成功', '今日已签到', '已签到']
    failure_keywords = ['签到失败', '错误', 'error', '失败', '权限不足', '无权进行此操作', '响应:']
    if '响应:' in text and '状态码:' not in text and '结果:' not in text:
        return False
    if any(keyword in text for keyword in failure_keywords) and '已签到' not in text:
        return False
    if any(keyword in text for keyword in success_keywords) or 'success' in lowered:
        return True
    return False


def summarize_checkin_output(output: str):
    text = (output or '').strip()
    if not text:
        return ['(无输出)']

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    meaningful = [
        line for line in lines
        if any(keyword in line for keyword in ['分类', '签到', '成功', '失败', '状态码', '结果', '错误', '已签到', '用户 ID', '站点', '响应', 'Traceback', 'WARNING:', 'ERROR:'])
    ]
    return meaningful or lines[:8]


def cmd_qd(raw: str):
    name = raw.strip()
    target_name = None if name in {'', 'all', '*'} else name
    sites = load_sites()
    if target_name:
        sites = [s for s in sites if s['name'] == target_name]
        if not sites:
            return f'❌ 未找到站点: [{target_name}]'

    if not sites:
        return '❌ 未配置任何站点。先用 /api_add 添加。'

    if not CHECKIN_SCRIPT.exists():
        return f'❌ 签到脚本不存在: {CHECKIN_SCRIPT}'

    results = []
    success_count = 0
    completed_count = 0
    today = today_str()

    for site in sites:
        results.append(f'【{site["name"]}】')
        try:
            completed = run_checkin_with_fallback(site)
            output = completed.stdout if completed.stdout.strip() else completed.stderr
            results.extend(summarize_checkin_output(output))

            completed_count += 1
            success_state = detect_success_state(output)
            if is_checkin_success(completed, output):
                success_count += 1
                if success_state:
                    classification = 'already_checked_in' if success_state == 'already_checked_in' else 'success'
                    update_cached_status(site['name'], {
                        'date': today,
                        'state': success_state,
                        'classification': classification,
                        'message': '今日已签到' if success_state == 'already_checked_in' else '签到成功',
                        'updated_at': subprocess.run(
                            ['date', '+%F %T'],
                            capture_output=True,
                            text=True,
                            timeout=5,
                            cwd='/root',
                        ).stdout.strip(),
                    })
            else:
                results.append(f'退出码: {completed.returncode}')
        except subprocess.TimeoutExpired:
            results.append('⏰ 签到超时（120s）')
        except FileNotFoundError:
            results.append(f'❌ Python 环境未找到')
        except Exception as exc:
            results.append(f'❌ 错误: {exc}')
        results.append('')

    results.append(f'汇总: 成功 {success_count}/{len(sites)}，已执行 {completed_count}/{len(sites)}')
    return '\n'.join(results).strip()


def main():
    parser = argparse.ArgumentParser(description='API 签到管理', exit_on_error=False)
    parser.add_argument('action', nargs='?', default='list')
    parser.add_argument('args', nargs=argparse.REMAINDER, default=[])
    parsed = parser.parse_args()

    structured = split_structured_command(' '.join([parsed.action, *parsed.args]).strip())
    if structured and structured[0] in {'add', 'del', 'qd', 'list'}:
        action = structured[0]
        raw = structured[1] if len(structured) > 1 else ''
    else:
        action = parsed.action
        raw = ' '.join(parsed.args)

    if raw.startswith('-- '):
        raw = raw[3:]
    elif raw == '--':
        raw = ''

    if action == 'list':
        print(cmd_list())
    elif action == 'add':
        print(cmd_add(raw))
    elif action == 'del':
        print(cmd_del(raw))
    elif action == 'qd':
        print(cmd_qd(raw))
    else:
        parser.error(f'unknown action: {action}')


if __name__ == '__main__':
    main()
