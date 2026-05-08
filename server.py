#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - 视频号批量上传工具 Web 服务器 (Flask + Flask-SocketIO)
Translated from server.js
"""

import asyncio
import csv
import json
import hashlib
import os
import random
import re
import shutil
import ssl
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory, session
from flask_socketio import SocketIO, emit

try:
    import certifi
except Exception:
    certifi = None

from accounts import (
    loadAccounts,
    getAccount,
    updateAccountStatus,
)
from batch_upload import (
    init_browser,
    unlock_profile,
    batch_upload,
    preflight_records,
    load_csv_from_string,
    detect_login_state,
    upload_headless_from_env,
    info_should_show_in_live_ui,
    set_ui_event_handler,
    logger as batch_logger,
    LOG_PATH,
    RESULTS_PATH,
    SCREENSHOTS_DIR,
)

BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
RES_DIR = os.environ.get('APP_RES_DIR', BASE_DIR)

app = Flask(__name__, static_folder=os.path.join(RES_DIR, 'public'), static_url_path='')
app.secret_key = os.environ.get('APP_SECRET_KEY') or os.environ.get('SECRET_KEY') or ''.join(
    random.choice(string.ascii_letters + string.digits) for _ in range(48)
)
# 拖拽上传视频走 multipart，超过此值会 413；默认 2GB，可用环境变量 MAX_UPLOAD_MB（单位 MB）覆盖
_max_upload_mb = int(os.environ.get('MAX_UPLOAD_MB', str(2 * 1024)))
app.config['MAX_CONTENT_LENGTH'] = _max_upload_mb * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

PORT = int(os.environ.get('PORT', 3123))
UPLOADS_DIR = os.path.join(BASE_DIR, 'uploads')
LAST_BATCH_PATH = os.path.join(BASE_DIR, 'last-batch.csv')
PRIMARY_ACCOUNT_NAME = 'default'

os.makedirs(UPLOADS_DIR, exist_ok=True)

# ── State ──
active_contexts = {}  # {name: browser_context}
upload_state = {'running': False, 'abort': False, 'account': None}

# Playwright 的 BrowserContext 必须在与创建它相同的 asyncio 事件循环上操作。
# 上传在独立线程里跑 loop；验证若用 Flask 线程里新建的 loop 去 new_page/goto，常见结果是新标签一直 about:blank。
_browser_loop_lock = threading.Lock()
_browser_loops = {}  # account_name -> asyncio.AbstractEventLoop（持有该账号 context 的 loop）
_browser_stop_events = {}  # account_name -> asyncio.Event（api_close 时 set，结束 _upload_session 里的 wait）

# ── Socket.IO broadcast ──


def broadcast(msg):
    """Send a message to all connected Socket.IO clients."""
    try:
        socketio.emit('message', msg, namespace='/')
    except Exception:
        pass


def _forward_ui_event(event):
    if not isinstance(event, dict):
        return
    payload = dict(event)
    payload.setdefault('ts', datetime.now(timezone.utc).isoformat())
    broadcast(payload)


# Override logger to broadcast via WebSocket
_orig_info = batch_logger.info
_orig_warn = batch_logger.warn
_orig_error = batch_logger.error


def _broadcast_info(msg):
    # 先落盘/终端（全量）；实时日志只推白名单，避免与控制台「改反了」
    _orig_info(msg)
    if not info_should_show_in_live_ui(msg):
        return
    broadcast({
        'type': 'log',
        'level': 'INFO',
        'msg': msg,
        'ts': datetime.now(timezone.utc).isoformat()
    })


def _broadcast_warn(msg):
    _orig_warn(msg)
    broadcast({
        'type': 'log',
        'level': 'WARN',
        'msg': msg,
        'ts': datetime.now(timezone.utc).isoformat()
    })


def _broadcast_error(msg):
    _orig_error(msg)
    broadcast({
        'type': 'log',
        'level': 'ERROR',
        'msg': msg,
        'ts': datetime.now(timezone.utc).isoformat()
    })


batch_logger.info = _broadcast_info
batch_logger.warn = _broadcast_warn
batch_logger.error = _broadcast_error
set_ui_event_handler(_forward_ui_event)

logger = batch_logger

# ── Auto-shutdown when no clients connected ──

connected_clients = set()
shutdown_timer = None
SHUTDOWN_DELAY = 30  # seconds
AUTH_LOGIN_URL = 'http://47.114.217.61:9988/api/v1/admin/login'
AUTH_USER_INFO_URL = 'http://47.114.217.61:9988/api/v1/admin/user-info'
DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
UPDATE_USER_AGENT = 'wechat-channels-uploader/1.0'
AUTH_STATUS_GRACE_SECONDS = 8
PUBLIC_API_PATHS = {
    '/api/auth/login',
    '/api/auth/status',
    '/api/auth/logout',
    '/api/version',
    '/api/update/check',
    '/api/update/download',
    '/api/update/status',
    '/api/update/restart',
}
update_state_lock = threading.Lock()
update_state = {}


def reset_update_state():
    with update_state_lock:
        update_state.clear()
        update_state.update({
            'running': False,
            'status': 'idle',
            'stage': 'idle',
            'progress': 0,
            'indeterminate': False,
            'message': '',
            'error': '',
            'version': '',
            'path': '',
            'open_target': '',
            'started_at': '',
            'finished_at': '',
            'restart_ready': False,
            'restarting': False,
        })


def set_update_state(**kwargs):
    with update_state_lock:
        update_state.update(kwargs)


def get_update_state():
    with update_state_lock:
        return dict(update_state)


reset_update_state()


def is_authenticated():
    return bool(session.get('auth_user'))


def ensure_primary_account_name(name):
    return (name or '').strip() == PRIMARY_ACCOUNT_NAME


def _is_expected_closed_error(err) -> bool:
    msg = str(err or '').lower()
    if not msg:
        return False
    markers = (
        'target page, context or browser has been closed',
        'browser has been closed',
        'context has been closed',
        'page has been closed',
        'frame was detached',
    )
    return any(m in msg for m in markers)


def parse_version_tuple(version):
    parts = re.findall(r'\d+', str(version or '0'))
    nums = [int(p) for p in parts[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)


def load_local_version_info():
    path = os.path.join(RES_DIR, 'version.json')
    if not os.path.exists(path):
        path = os.path.join(BASE_DIR, 'version.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'version': '0.0.0'}


def load_update_config():
    path = os.path.join(RES_DIR, 'update.json')
    if not os.path.exists(path):
        path = os.path.join(BASE_DIR, 'update.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def get_platform_name():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    return sys.platform


def create_ssl_context():
    # 始终返回一个不验证域名的 SSL Context（用于内网或自有域名的自签/测试证书）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def urlopen_with_context(req, timeout=15):
    target_url = ''
    if isinstance(req, urllib.request.Request):
        target_url = req.full_url or ''
    else:
        target_url = str(req or '')
    if str(target_url).lower().startswith('https://'):
        return urllib.request.urlopen(req, timeout=timeout, context=create_ssl_context())
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_remote_json(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UPDATE_USER_AGENT})
        with urlopen_with_context(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
        return json.loads(raw)
    except Exception as e:
        print(f"Error fetching remote JSON from {url}: {e}")
        return None


def resolve_update_info():
    cfg = load_update_config()
    version_info = load_local_version_info()
    current_version = str(version_info.get('version') or '0.0.0')
    platform_name = get_platform_name()
    manifest_url = str(cfg.get('manifest_url') or '').strip()
    if not manifest_url:
        return {
            'enabled': False,
            'current_version': current_version,
            'platform': platform_name,
            'message': '未配置更新地址',
        }

    manifest = fetch_remote_json(manifest_url, timeout=20)
    if not isinstance(manifest, dict) or not manifest:
        return {
            'enabled': False,
            'current_version': current_version,
            'platform': platform_name,
            'manifest_url': manifest_url,
            'message': '更新清单获取失败，请检查远端 manifest.json 是否可访问',
        }

    latest_version = str(manifest.get('latest_version') or current_version)
    min_supported_version = str(manifest.get('min_supported_version') or latest_version)
    force = bool(manifest.get('force'))
    downloads = manifest.get('downloads') or {}
    platform_download = downloads.get(platform_name) if isinstance(downloads, dict) else None
    update_required = force or parse_version_tuple(current_version) < parse_version_tuple(min_supported_version)
    update_available = parse_version_tuple(current_version) < parse_version_tuple(latest_version)

    return {
        'enabled': True,
        'current_version': current_version,
        'latest_version': latest_version,
        'min_supported_version': min_supported_version,
        'force': force,
        'required': update_required,
        'available': update_available,
        'platform': platform_name,
        'notes': manifest.get('notes') or '',
        'download_url': platform_download,
        'manifest_url': manifest_url,
        'sha256': (manifest.get('sha256') or {}).get(platform_name, ''),
    }


def download_update_package(download_url, version, progress_callback=None):
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    platform_name = get_platform_name()
    filename = os.path.basename(urllib.parse.urlparse(download_url).path) or f'update-{platform_name}-{version}.zip'
    local_path = os.path.join(DOWNLOADS_DIR, filename)
    req = urllib.request.Request(download_url, headers={'User-Agent': UPDATE_USER_AGENT})
    with urlopen_with_context(req, timeout=120) as resp, open(local_path, 'wb') as f:
        try:
            total = int(resp.headers.get('Content-Length') or '0')
        except Exception:
            total = 0
        downloaded = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if progress_callback:
                progress_callback(downloaded, total, filename)
    return local_path


def sha256_file(file_path):
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def launch_update_target(file_path):
    if sys.platform == 'win32':
        os.startfile(file_path)
        return
    if sys.platform == 'darwin':
        subprocess.Popen(['open', file_path])
        return
    subprocess.Popen(['xdg-open', file_path])


def _restart_app_process(open_target):
    try:
        launch_update_target(open_target)
    except Exception as e:
        set_update_state(
            running=False,
            status='failed',
            stage='failed',
            indeterminate=False,
            message='启动新版本失败',
            error=str(e),
            restart_ready=True,
            restarting=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    time.sleep(1.2)
    try:
        cleanup()
    except Exception:
        pass
    os._exit(0)


def get_update_extract_dir(version):
    safe_version = re.sub(r'[^0-9A-Za-z._-]+', '-', str(version or 'latest')).strip('-') or 'latest'
    return os.path.join(DOWNLOADS_DIR, 'extracted', f'{get_platform_name()}-v{safe_version}')


def safe_extract_zip(zip_fp, dest_dir):
    dest_real = os.path.realpath(dest_dir)
    for member in zip_fp.infolist():
        member_name = member.filename.replace('\\', '/')
        target_path = os.path.realpath(os.path.join(dest_dir, member_name))
        if os.path.commonpath([dest_real, target_path]) != dest_real:
            raise ValueError('更新包包含非法路径，已拒绝解压')
        zip_fp.extract(member, dest_dir)


def find_update_launch_target(root_dir):
    if sys.platform == 'darwin':
        for dirpath, dirnames, _ in os.walk(root_dir):
            for dirname in sorted(dirnames):
                if dirname.endswith('.app'):
                    return os.path.join(dirpath, dirname)

    exe_candidates = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if '_internal' in dirpath.split(os.sep):
            continue
        if sys.platform == 'win32':
            for filename in sorted(filenames):
                if not filename.lower().endswith('.exe'):
                    continue
                full_path = os.path.join(dirpath, filename)
                if '视频号批量上传' in filename:
                    return full_path
                exe_candidates.append(full_path)

    if exe_candidates:
        return exe_candidates[0]
    return root_dir


def prepare_downloaded_update(file_path, version):
    if str(file_path).lower().endswith('.zip'):
        extract_dir = get_update_extract_dir(version)
        if os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(file_path, 'r') as zf:
            safe_extract_zip(zf, extract_dir)
        return {
            'path': file_path,
            'extract_dir': extract_dir,
            'open_target': find_update_launch_target(extract_dir),
        }
    return {
        'path': file_path,
        'extract_dir': '',
        'open_target': file_path,
    }


def login_response_ok(status_code, payload):
    if not (200 <= status_code < 300):
        return False
    if not isinstance(payload, dict):
        return True

    if payload.get('success') is False:
        return False

    code = payload.get('code')
    if code is not None:
        if isinstance(code, bool):
            return code
        if isinstance(code, (int, float)):
            if code not in (0, 200):
                return False
        elif str(code).lower() not in ('0', '200', 'ok', 'success'):
            return False

    status = payload.get('status')
    if isinstance(status, str) and status.lower() not in ('ok', 'success', '200'):
        return False

    return True


def extract_login_error(status_code, payload, raw_text):
    if isinstance(payload, dict):
        for key in ('msg', 'message', 'error', 'detail'):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    if raw_text and raw_text.strip():
        return raw_text.strip()[:200]
    if status_code == 401:
        return '用户名或密码错误'
    return f'登录失败（{status_code}）'


def _find_token_in_payload(obj):
    if isinstance(obj, dict):
        for key in ('token', 'accessToken', 'access_token', 'jwt', 'authorization'):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in obj.values():
            token = _find_token_in_payload(val)
            if token:
                return token
    elif isinstance(obj, list):
        for item in obj:
            token = _find_token_in_payload(item)
            if token:
                return token
    return ''


def extract_auth_token(payload):
    token = _find_token_in_payload(payload if isinstance(payload, (dict, list)) else {})
    return token.strip()


def clear_auth_session():
    session.pop('auth_user', None)
    session.pop('auth_payload', None)
    session.pop('auth_token', None)
    session.pop('auth_login_at', None)
    session.modified = True


def validate_remote_auth_token(token):
    token = str(token or '').strip()
    if not token:
        return False, 'missing_token'

    req = urllib.request.Request(
        AUTH_USER_INFO_URL,
        headers={
            'token': token,
            'User-Agent': UPDATE_USER_AGENT,
        },
        method='GET',
    )
    try:
        with urlopen_with_context(req, timeout=15) as resp:
            status_code = resp.getcode()
            raw = resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        status_code = e.code
        raw = e.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return True, f'validation_unavailable:{e}'

    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}

    if status_code == 401:
        return False, 'unauthorized'
    if isinstance(payload, dict):
        code = payload.get('code')
        message = str(payload.get('message') or payload.get('msg') or '').strip()
        if code == 10024 or '没登录' in message or '请先登录' in message:
            return False, 'expired'
        if payload.get('status') == 200 or code in (0, 200):
            return True, ''
    if 200 <= status_code < 300:
        return True, ''
    return True, ''


def remote_auth_status(token):
    token = str(token or '').strip()
    if not token:
        return False, 'missing_token', {}

    req = urllib.request.Request(
        AUTH_USER_INFO_URL,
        headers={
            'token': token,
            'User-Agent': UPDATE_USER_AGENT,
        },
        method='GET',
    )
    try:
        with urlopen_with_context(req, timeout=15) as resp:
            status_code = resp.getcode()
            raw = resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        status_code = e.code
        raw = e.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return True, f'validation_unavailable:{e}', {}

    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}

    if status_code == 401:
        return False, 'unauthorized', payload if isinstance(payload, dict) else {}

    if isinstance(payload, dict):
        code = payload.get('code')
        message = str(payload.get('message') or payload.get('msg') or '').strip()
        if code == 10024 or '没登录' in message or '请先登录' in message:
            return False, 'expired', payload
        if payload.get('status') == 200 or code in (0, 200):
            return True, '', payload

    if 200 <= status_code < 300:
        return True, '', payload if isinstance(payload, dict) else {}
    return True, '', payload if isinstance(payload, dict) else {}


def _request_login(payload_bytes, content_type):
    req = urllib.request.Request(
        AUTH_LOGIN_URL,
        data=payload_bytes,
        headers={'Content-Type': content_type},
        method='POST',
    )

    try:
        with urlopen_with_context(req, timeout=15) as resp:
            return resp.getcode(), resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='ignore')


def authenticate_remote(username, password):
    try:
        attempts = [
            (
                json.dumps({'username': username, 'password': password}).encode('utf-8'),
                'application/json',
            ),
            (
                urllib.parse.urlencode({'username': username, 'password': password}).encode('utf-8'),
                'application/x-www-form-urlencoded',
            ),
        ]
        status_code = 0
        raw = ''
        payload = None
        for body, content_type in attempts:
            status_code, raw = _request_login(body, content_type)
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = None
            if login_response_ok(status_code, payload):
                return True, payload if isinstance(payload, dict) else {}, payload
    except Exception as e:
        return False, {'message': f'登录服务不可用: {e}'}, None

    if not login_response_ok(status_code, payload):
        return False, {'message': extract_login_error(status_code, payload, raw)}, payload

    return True, payload if isinstance(payload, dict) else {}, payload


@app.before_request
def require_auth():
    path = request.path or '/'
    if path.startswith('/socket.io'):
        return None
    if not path.startswith('/api/'):
        return None
    if path in PUBLIC_API_PATHS:
        return None
    if is_authenticated():
        return None
    return jsonify({'error': '未登录或登录已失效'}), 401


@socketio.on('connect')
def on_connect():
    if not is_authenticated():
        return False
    connected_clients.add(request.sid)
    global shutdown_timer
    if shutdown_timer is not None:
        shutdown_timer.cancel()
        shutdown_timer = None
    logger.info(f'Client connected ({len(connected_clients)} total)')


@socketio.on('disconnect')
def on_disconnect():
    connected_clients.discard(request.sid)
    global shutdown_timer
    if len(connected_clients) == 0:

        def _do_shutdown():
            if len(connected_clients) == 0:
                upload_state['abort'] = True
                logger.info('No clients connected, shutting down...')
                cleanup()
                os._exit(0)

        shutdown_timer = threading.Timer(SHUTDOWN_DELAY, _do_shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()
    logger.info(f'Client disconnected ({len(connected_clients)} total)')


# ── Helper: run async coroutine ──


def run_async_sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def run_async_thread(coro):
    """Run an async coroutine in a background daemon thread (non-blocking)."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f'Background task error: {e}')
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── Cleanup ──


def cleanup():
    """Close all active browser contexts."""
    for name in list(active_contexts.keys()):
        ctx = active_contexts.get(name)
        loop = None
        with _browser_loop_lock:
            loop = _browser_loops.get(name)
        if ctx is not None and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    _async_close_account_browser(name), loop
                ).result(timeout=25)
            except Exception:
                pass
        elif ctx is not None:
            active_contexts.pop(name, None)
            nl = asyncio.new_event_loop()
            asyncio.set_event_loop(nl)
            try:
                nl.run_until_complete(ctx.close())
            except Exception:
                pass
            finally:
                nl.close()


def _prepare_headless_upload_profile(profile_dir):
    """macOS 无头上传时复制一份精简 profile，避免直接复用真实目录导致 Chromium 崩溃。"""
    src = os.path.abspath(profile_dir)
    temp_root = tempfile.mkdtemp(prefix='wx-headless-profile-')
    dst = os.path.join(temp_root, 'profile')
    try:
        _copy_profile_tree(src, dst)
        logger.info(f'Headless upload profile prepared: {dst}')
        return dst, temp_root
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _is_volatile_profile_entry(name):
    name = str(name or '')
    if not name:
        return False
    volatile_names = {
        'Cache', 'Code Cache', 'GPUCache', 'ShaderCache', 'GrShaderCache',
        'GraphiteDawnCache', 'DawnCache', 'Crashpad', 'BrowserMetrics',
        'SingletonLock', 'SingletonCookie', 'SingletonSocket', 'LOCK', '.lock',
        'RunningChromeVersion', 'Last Version', 'DevToolsActivePort',
    }
    if name in volatile_names:
        return True
    if name.endswith('.lock'):
        return True
    if name.startswith('Singleton'):
        return True
    if name.startswith('RunningChrome'):
        return True
    return False


def _profile_copy_ignore(_src, _names):
    return {n for n in _names if _is_volatile_profile_entry(n)}


def _copy_profile_file(src, dst, *, follow_symlinks=True):
    try:
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        logger.warn(f'Profile copy skipped vanished file: {src}')
        return dst


def _copy_profile_tree(src, dst):
    shutil.copytree(
        src,
        dst,
        dirs_exist_ok=True,
        ignore=_profile_copy_ignore,
        copy_function=_copy_profile_file,
    )




def _prepare_visible_browser_profile(profile_dir):
    """Create a temporary visible-browser profile copy and sync it back on close."""
    src = os.path.abspath(profile_dir)
    temp_root = tempfile.mkdtemp(prefix='wx-visible-profile-')
    dst = os.path.join(temp_root, 'profile')
    try:
        _copy_profile_tree(src, dst)
        logger.info(f'Visible browser profile prepared: {dst}')
        return dst, temp_root
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise

def _sync_profile_back(temp_profile_dir, target_profile_dir):
    """Merge login/session data from temporary profile back to the real profile."""
    if not temp_profile_dir or not target_profile_dir:
        return False
    src = os.path.abspath(temp_profile_dir)
    dst = os.path.abspath(target_profile_dir)
    # 临时目录可能已被系统或上游清理，属于可预期场景：直接跳过，不视为错误。
    if not os.path.isdir(src):
        return False
    os.makedirs(dst, exist_ok=True)
    _copy_profile_tree(src, dst)
    return True


def _cleanup_temp_profile(ctx, persist_back=False):
    """Cleanup per-context temp profile; optionally sync it back first."""
    if ctx is None:
        return
    temp_root = getattr(ctx, '_wx_temp_profile_root', None)
    temp_profile_dir = getattr(ctx, '_wx_temp_profile_dir', None)
    target_profile_dir = getattr(ctx, '_wx_origin_profile_dir', None)
    if persist_back and temp_profile_dir and target_profile_dir:
        try:
            synced = _sync_profile_back(temp_profile_dir, target_profile_dir)
            if synced:
                logger.info(f'Profile synced back to: {target_profile_dir}')
        except Exception as e:
            logger.warn(f'Profile sync-back failed: {e}')
    if temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)


def _kill_mac_browser_processes_for_profile(profile_dir):
    """Best-effort cleanup for macOS browser processes still holding this profile."""
    if sys.platform != 'darwin':
        return
    abs_profile_dir = os.path.abspath(profile_dir)
    escaped = re.escape(abs_profile_dir)
    patterns = [
        rf'Chromium.*--user-data-dir[= ]{escaped}',
        rf'Google Chrome.*--user-data-dir[= ]{escaped}',
        rf'chrome.*--user-data-dir[= ]{escaped}',
    ]
    for pattern in patterns:
        try:
            subprocess.run(
                ['pkill', '-f', pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def _close_account_browser_for_upload_start(name, acct):
    """上传前强制关闭已打开浏览器（不抛错），确保上传队列可直接继续。"""
    ctx = active_contexts.get(name)
    with _browser_loop_lock:
        loop = _browser_loops.get(name)

    if ctx is not None and loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(
                _async_close_account_browser(name), loop
            ).result(timeout=45)
        except Exception as e:
            logger.warn(f'upload_start: close existing browser failed, continue anyway: {e}')
    else:
        ex = active_contexts.pop(name, None)
        if ex is not None:
            try:
                run_async_sync(ex.close())
            except Exception:
                pass

    # 保险：把残留进程也清掉，避免 profile 被占用
    try:
        if sys.platform == 'darwin':
            _kill_mac_browser_processes_for_profile(acct['profileDir'])
    except Exception:
        pass


# ══════════════════════════════════════════════
# API Routes
# ══════════════════════════════════════════════

# ── Auth ──


@app.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    user = session.get('auth_user')
    token = session.get('auth_token')
    login_at_raw = session.get('auth_login_at') or ((user or {}).get('loginAt') if isinstance(user, dict) else None)
    if user and token:
        grace_active = False
        if login_at_raw:
            try:
                login_at = datetime.fromisoformat(str(login_at_raw).replace('Z', '+00:00'))
                grace_active = (datetime.now(timezone.utc) - login_at).total_seconds() < AUTH_STATUS_GRACE_SECONDS
            except Exception:
                grace_active = False

        if grace_active:
            return jsonify({
                'authenticated': True,
                'user': user,
                'grace': True,
            })

        valid, reason, payload = remote_auth_status(token)
        if not valid:
            clear_auth_session()
            return jsonify({
                'authenticated': False,
                'user': None,
                'expired': True,
                'reason': reason,
            })

        if isinstance(payload, dict):
            session['auth_payload'] = payload
            session.modified = True
    return jsonify({
        'authenticated': bool(user and token),
        'user': user,
    })


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username or not password:
        return jsonify({'error': '请输入用户名和密码'}), 400

    ok, info, raw_payload = authenticate_remote(username, password)
    if not ok:
        return jsonify({'error': info.get('message', '登录失败')}), 401

    token = extract_auth_token(raw_payload)
    if not token:
        return jsonify({'error': '登录成功但未获取到 token，请联系管理员检查登录接口返回'}), 401

    session['auth_user'] = {
        'username': username,
        'loginAt': datetime.now(timezone.utc).isoformat(),
    }
    session['auth_payload'] = raw_payload if isinstance(raw_payload, dict) else {}
    session['auth_token'] = token
    session['auth_login_at'] = session['auth_user']['loginAt']
    session.modified = True
    return jsonify({
        'message': '登录成功',
        'user': session['auth_user'],
    })


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    clear_auth_session()
    return jsonify({'message': '已退出登录'})


# ── Accounts ──


@app.route('/api/accounts', methods=['GET'])
def api_get_accounts():
    return jsonify(loadAccounts())


@app.route('/api/accounts', methods=['POST'])
def api_create_account():
    return jsonify({'error': '当前为单账号模式，不能新增账号'}), 403


@app.route('/api/accounts/<name>', methods=['PATCH'])
def api_update_account(name):
    return jsonify({'error': '当前为单账号模式，不能修改账号配置'}), 403


@app.route('/api/accounts/<name>', methods=['DELETE'])
def api_delete_account(name):
    return jsonify({'error': '当前为单账号模式，不能删除主账号'}), 403


# ── Login: launch native browser window for QR scan ──

@app.route('/api/accounts/<name>/open_browser', methods=['POST'])
def api_open_browser(name):
    """Launch a visible Chrome window to operate the account directly."""
    try:
        if upload_state.get('running'):
            return jsonify({'error': '当前有上传任务进行中，上传完成后才可打开账号浏览器'}), 409
        if not ensure_primary_account_name(name):
            return jsonify({'error': '当前为单账号模式，仅支持主账号'}), 403
        acct = getAccount(name)
        if acct is None:
            return jsonify({'error': '账号不存在'}), 404

        if active_contexts.get(name) is not None:
            with _browser_loop_lock:
                loop = _browser_loops.get(name)
            if loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        _async_close_account_browser(name), loop
                    ).result(timeout=90)
                except Exception as e:
                    logger.warn(f'open_browser: close existing browser: {e}')
            else:
                ex = active_contexts.pop(name, None)
                if ex is not None:
                    run_async_sync(ex.close())

        run_async_thread(_open_browser_async(name, acct))
        return jsonify({'message': '已打开账号浏览器'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

async def _open_browser_async(name, acct):
    await unlock_profile(acct['profileDir'])
    _kill_mac_browser_processes_for_profile(acct['profileDir'])
    logger.info(f'Open browser: launching visible browser for {acct["label"]}')
    launch_profile_dir = acct['profileDir']
    temp_profile_root = None
    if sys.platform == 'darwin':
        launch_profile_dir, temp_profile_root = _prepare_visible_browser_profile(acct['profileDir'])

    ctx = await init_browser(launch_profile_dir, headless=False)
    if temp_profile_root:
        ctx._wx_temp_profile_root = temp_profile_root
        ctx._wx_temp_profile_dir = launch_profile_dir
        ctx._wx_origin_profile_dir = acct['profileDir']

    if len(ctx.pages) == 0:
        await ctx.close()
        _cleanup_temp_profile(ctx, persist_back=True)
        return

    active_contexts[name] = ctx
    with _browser_loop_lock:
        _browser_loops[name] = asyncio.get_running_loop()
    page = ctx.pages[0]

    try:
        await page.goto('https://channels.weixin.qq.com/platform', wait_until='domcontentloaded')
        
        while active_contexts.get(name) is ctx:
            if not ctx.pages:
                break
            await asyncio.sleep(2)
    except Exception as e:
        logger.error(f'Open browser error: {e}')
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        _cleanup_temp_profile(ctx, persist_back=True)
        if active_contexts.get(name) is ctx:
            active_contexts.pop(name, None)
        with _browser_loop_lock:
            if _browser_loops.get(name) is asyncio.get_running_loop():
                _browser_loops.pop(name, None)


@app.route('/api/accounts/<name>/login', methods=['POST'])
def api_login(name):
    """Launch a visible Chrome window for WeChat QR login.
    Polls page URL every 2s; closes window and updates status on success."""
    try:
        if upload_state.get('running'):
            return jsonify({'error': '当前有上传任务进行中，上传完成后才可扫码登录'}), 409
        if not ensure_primary_account_name(name):
            return jsonify({'error': '当前为单账号模式，仅支持主账号'}), 403
        acct = getAccount(name)
        if acct is None:
            return jsonify({'error': '账号不存在'}), 404

        # Close any existing context for this account（须在与创建 context 相同的 loop 上 close）
        if active_contexts.get(name) is not None:
            with _browser_loop_lock:
                loop = _browser_loops.get(name)
            if loop is not None and not loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(
                        _async_close_account_browser(name), loop
                    ).result(timeout=90)
                except Exception as e:
                    logger.warn(f'login: close existing browser: {e}')
            else:
                ex = active_contexts.pop(name, None)
                if ex is not None:
                    run_async_sync(ex.close())

        run_async_thread(_login_async(name, acct))
        return jsonify({'message': '已开始扫码登录'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


async def _login_async(name, acct):
    """Launch browser, wait for QR scan completion, then close."""
    await unlock_profile(acct['profileDir'])
    _kill_mac_browser_processes_for_profile(acct['profileDir'])
    logger.info(f'Login browser: launching visible browser for {acct["label"]}')
    launch_profile_dir = acct['profileDir']
    temp_profile_root = None
    if sys.platform == 'darwin':
        launch_profile_dir, temp_profile_root = _prepare_visible_browser_profile(acct['profileDir'])

    ctx = await init_browser(launch_profile_dir, headless=False)
    if temp_profile_root:
        ctx._wx_temp_profile_root = temp_profile_root
        ctx._wx_temp_profile_dir = launch_profile_dir
        ctx._wx_origin_profile_dir = acct['profileDir']

    if len(ctx.pages) == 0:
        await ctx.close()
        _cleanup_temp_profile(ctx, persist_back=True)
        logger.error(f'Login browser failed to start for {acct["label"]}')
        return

    active_contexts[name] = ctx
    with _browser_loop_lock:
        _browser_loops[name] = asyncio.get_running_loop()
    page = ctx.pages[0]

    try:
        await page.goto('https://channels.weixin.qq.com/login.html',
                        wait_until='domcontentloaded', timeout=15000)
        # 等待页面稳定，client-side redirect 可能在 DOMContentLoaded 之后才触发
        await page.wait_for_timeout(3000)

        logger.info(f'Login window opened for {acct["label"]} (url: {page.url})')

        # 检查是否已经处于登录状态（不在 login 页面则已登录，只检查主页面）
        login_state = await detect_login_state(page)
        login_done = login_state.get('logged_in', False)
        if login_done:
            logger.info(f'Already logged in for {acct["label"]} (url: {page.url})')

        # Poll URL every 2s until login completes or expires or 5min timeout
        deadline = time.time() + 300

        while time.time() < deadline and not login_done:
            # 如果用户手动关闭了浏览器或页面，立刻退出扫码状态
            if page.is_closed() or not ctx.pages:
                logger.info(f'Login window closed manually for {acct["label"]}')
                broadcast({
                    'type': 'login-result',
                    'account': name,
                    'result': 'closed'
                })
                return

            await asyncio.sleep(2)

            login_state = await detect_login_state(page)
            if login_state.get('logged_in'):
                login_done = True
                break

            # Check for QR expiration on page
            try:
                body_text = await page.text_content('body') or ''
                expired_texts = ['过期', 'expired', '已失效', '重新获取']
                if any(t in body_text for t in expired_texts):
                    logger.info(f'QR code expired for {acct["label"]}')
                    broadcast({
                        'type': 'login-result',
                        'account': name,
                        'result': 'expired'
                    })
                    return
            except Exception:
                pass

            # Check for scanned state
            try:
                body_text = await page.text_content('body') or ''
                if '已扫码' in body_text or '扫描成功' in body_text or '确认登录' in body_text:
                    logger.info(f'QR scanned for {acct["label"]}')
            except Exception:
                pass

        if login_done:
            updateAccountStatus(name, 'ready')
            logger.info(f'Login complete for {acct["label"]}')
            broadcast({
                'type': 'account-updated',
                'account': name,
                'status': 'ready'
            })
        else:
            logger.warn(f'Login timeout for {acct["label"]}')
            broadcast({
                'type': 'login-result',
                'account': name,
                'result': 'timeout'
            })

    except Exception as e:
        if _is_expected_closed_error(e):
            # 浏览器被手动关闭或无头进程退出时属于预期噪音，按 closed 处理即可
            broadcast({
                'type': 'login-result',
                'account': name,
                'result': 'closed'
            })
            return
        logger.error(f'Login error for {acct["label"]}: {e}')
        broadcast({
            'type': 'login-result',
            'account': name,
            'result': 'error',
            'error': str(e)
        })
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        _cleanup_temp_profile(ctx, persist_back=True)
        if active_contexts.get(name) is ctx:
            active_contexts.pop(name, None)
        with _browser_loop_lock:
            if _browser_loops.get(name) is asyncio.get_running_loop():
                _browser_loops.pop(name, None)


# ── Verify login status ──


async def _check_account_login_state(page):
    """验证账号登录态时，优先探测视频号后台页，避免根地址未完成跳转造成误判。"""
    probe_urls = [
        'https://channels.weixin.qq.com/platform',
        'https://channels.weixin.qq.com/platform/post/create',
    ]
    last_state = {'logged_in': False, 'reason': 'fallback', 'detail': page.url or ''}

    for probe_url in probe_urls:
        try:
            logger.info(f'verify: navigating to {probe_url} ...')
            await page.goto(probe_url, wait_until='domcontentloaded', timeout=20000)
            logger.info(f'verify: reached {probe_url}, current url: {page.url}')
        except Exception as e:
            err_msg = str(e)
            if not _is_expected_closed_error(err_msg):
                logger.warn(f'verify: goto {probe_url} failed: {err_msg}')
            if 'closed' in err_msg.lower() or 'terminate' in err_msg.lower():
                last_state = {'logged_in': False, 'reason': 'browser_crash', 'detail': err_msg}
                break
            continue

        for attempt in range(3):
            try:
                if page.is_closed():
                    break
                await page.wait_for_timeout(1500 if attempt == 0 else 1000)
            except Exception as e:
                if not _is_expected_closed_error(e):
                    logger.warn(f'verify: wait_for_timeout failed: {e}')
                last_state = {'logged_in': False, 'reason': 'browser_crash', 'detail': str(e)}
                break
            
            if page.is_closed():
                break
            last_state = await detect_login_state(page)
            if last_state.get('logged_in'):
                logger.info(
                    f'verify: logged in via {probe_url} '
                    f'({last_state.get("reason")} / {last_state.get("detail")})'
                )
                return last_state
            if last_state.get('reason') in ('login_url', 'login_text'):
                logger.info(
                    f'verify: logged out via {probe_url} '
                    f'({last_state.get("reason")} / {last_state.get("detail")})'
                )
                return last_state

    logger.info(
        'verify: final login probe result '
        f'({last_state.get("reason")} / {last_state.get("detail")})'
    )
    return last_state


@app.route('/api/accounts/<name>/verify', methods=['POST'])
def api_verify(name):
    try:
        if not ensure_primary_account_name(name):
            return jsonify({'error': '当前为单账号模式，仅支持主账号'}), 403
        acct = getAccount(name)
        if acct is None:
            return jsonify({'error': '账号不存在'}), 404

        ctx = active_contexts.get(name)
        with _browser_loop_lock:
            loop = _browser_loops.get(name)
        if ctx is not None and loop is not None and not loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(_verify_hot_path(name), loop)
                result = fut.result(timeout=90)
                return jsonify(result), 200
            except Exception as e:
                logger.error(f'verify (browser loop) exception: {e}')
                return jsonify({
                    'name': name,
                    'valid': False,
                    'error': str(e)[:400],
                    'hint': (
                        '无法在持有该浏览器的线程上完成验证（常见于 loop 已关闭）。'
                        '请点「关闭浏览器」后再点「验证」，或刷新页面后重试。'
                    ),
                }), 200

        result = run_async_sync(_verify_async(name, acct))
        return jsonify(result), 200
    except Exception as e:
        if not _is_expected_closed_error(e):
            logger.error(f'verify exception: {e}')
        return jsonify({
            'name': name,
            'valid': False,
            'error': str(e),
        }), 200


async def _verify_hot_path(name):
    """在创建 context 的同一条事件循环上验证（必须由 run_coroutine_threadsafe 投递到 _browser_loops[name]）。"""
    ctx = active_contexts.get(name)
    if ctx is None:
        return {'name': name, 'valid': False, 'error': '浏览器上下文丢失', 'hint': '请刷新后重试。'}

    if upload_state.get('running') and upload_state.get('account') == name:
        return {
            'name': name,
            'notice': (
                '该账号的自动化仍在运行：视频传完后脚本还可能正在填简介、选短剧、点「发表」等。'
                '请等任务完全结束后再点「验证」（日志里出现「SUCCESS」或「Upload complete: x/x」即已结束）。'
            ),
        }
    page = None
    last_err = None
    for attempt in range(3):
        try:
            page = await ctx.new_page()
            break
        except Exception as e:
            last_err = e
            logger.warn(f'verify: new_page attempt {attempt + 1}/3: {e}')
            await asyncio.sleep(0.5)
    if page is None:
        logger.error(f'verify: new_page failed after retries: {last_err}')
        return {
            'name': name,
            'valid': False,
            'error': str(last_err)[:400] if last_err else '新建验证页面失败',
            'hint': (
                '无法在已打开的浏览器里新建验证标签（见上方错误）。'
                '若你确认视频已传完但脚本仍卡住，可先点「停止上传」或在本账号卡片点「关闭浏览器」，再重新点「验证」。'
            ),
        }
    out = {'name': name, 'valid': False}
    try:
        login_state = await _check_account_login_state(page)
        expired = not login_state.get('logged_in', False)
        if expired:
            updateAccountStatus(name, 'needs-login')
        else:
            updateAccountStatus(name, 'ready')
        out = {
            'name': name,
            'valid': not expired,
            'reason': login_state.get('reason', ''),
            'detail': login_state.get('detail', ''),
        }
    except Exception as e:
        if not _is_expected_closed_error(e):
            logger.error(f'verify: goto/check failed: {e}')
        out = {
            'name': name,
            'valid': False,
            'error': str(e)[:400],
            'hint': (
                '验证页加载失败（见错误详情）。若上传已结束，可尝试「关闭浏览器」后重试。'
            ),
        }
    finally:
        try:
            if page is not None and not page.is_closed():
                await page.close()
        except Exception:
            pass
    return out


async def _verify_async(name, acct):
    """无活跃上传 loop 时：无头 launch 验证，或返回「上传占用」提示。"""

    # 上传任务占用同一账号浏览器时，不应再 launch 第二个 Chromium（会 Singleton 冲突）
    if upload_state.get('running') and upload_state.get('account') == name:
        return {
            'name': name,
            'notice': (
                '当前账号的自动化正在运行，浏览器已被占用，无法再起第二个验证窗口。'
                '请等任务结束（含传完视频后的填表、发表等）后再验证。'
            ),
        }

    # 强力解锁 Profile (不仅清理文件，还要清理可能残留的进程)
    await unlock_profile(acct['profileDir'])
    if sys.platform == 'darwin':
        _kill_mac_browser_processes_for_profile(acct['profileDir'])

    launch_profile_dir = acct['profileDir']
    temp_profile_root = None
    
    # macOS 下即使是验证也建议走临时 profile，避免 Singleton 冲突导致崩溃
    if sys.platform == 'darwin':
        try:
            launch_profile_dir, temp_profile_root = _prepare_headless_upload_profile(acct['profileDir'])
        except Exception as e:
            logger.warn(f'verify: prepare temp profile failed: {e}')

    ctx = None
    try:
        ctx = await init_browser(
            launch_profile_dir,
            headless=True,
            allow_visible_fallback=False,
        )
        if temp_profile_root:
            ctx._wx_temp_profile_root = temp_profile_root
            ctx._wx_temp_profile_dir = launch_profile_dir
    except Exception as e:
        if not _is_expected_closed_error(e):
            logger.error(f'verify: launch browser failed: {e}')
        if temp_profile_root:
            shutil.rmtree(temp_profile_root, ignore_errors=True)
        return {
            'name': name,
            'valid': False,
            'error': str(e),
            'hint': '无头验证启动失败。请先关闭残留浏览器后重试；若要人工查看，请使用「打开浏览器」。',
        }
    try:
        page = ctx.pages[0] if len(ctx.pages) > 0 else await ctx.new_page()
        login_state = await _check_account_login_state(page)
        expired = not login_state.get('logged_in', False)
        if expired:
            updateAccountStatus(name, 'needs-login')
        else:
            updateAccountStatus(name, 'ready')

        return {
            'name': name,
            'valid': not expired,
            'reason': login_state.get('reason', ''),
            'detail': login_state.get('detail', ''),
        }
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        if temp_profile_root:
            shutil.rmtree(temp_profile_root, ignore_errors=True)



async def _async_close_account_browser(name):
    """在持有 context 的 loop 上关闭浏览器并唤醒 _upload_session 的 wait（若存在）。"""
    ctx = active_contexts.get(name)
    if ctx is None:
        evt = _browser_stop_events.get(name)
        if evt is not None and not evt.is_set():
            evt.set()
        return
    try:
        await ctx.close()
    except Exception:
        pass
    _cleanup_temp_profile(ctx, persist_back=True)
    if active_contexts.get(name) is ctx:
        active_contexts.pop(name, None)
    with _browser_loop_lock:
        loop = _browser_loops.get(name)
        if loop is asyncio.get_running_loop():
            _browser_loops.pop(name, None)
    evt = _browser_stop_events.get(name)
    if evt is not None and not evt.is_set():
        evt.set()


# ── Close browser for an account ──


@app.route('/api/accounts/<name>/close', methods=['POST'])
def api_close(name):
    try:
        if not ensure_primary_account_name(name):
            return jsonify({'error': '当前为单账号模式，仅支持主账号'}), 403
        ctx = active_contexts.get(name)
        with _browser_loop_lock:
            loop = _browser_loops.get(name)

        if ctx is not None and loop is not None and not loop.is_closed():

            def _schedule():
                asyncio.ensure_future(_async_close_account_browser(name), loop=loop)

            loop.call_soon_threadsafe(_schedule)
            return jsonify({'message': '浏览器已关闭'})

        popped = active_contexts.pop(name, None)
        if popped is not None:
            run_async_sync(popped.close())
        evt = _browser_stop_events.get(name)
        if evt is not None and not evt.is_set() and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(evt.set)
            except Exception:
                pass
        return jsonify({'message': '浏览器已关闭'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Upload ──


async def _upload_session(account_name, csv_content, schedule_interval_min=None):
    """跑上传；结束后立刻关闭 BrowserContext。

    历史上这里曾让 loop 挂着（`await stop_evt.wait()`）以便用户在上传完后还能用同一个
    浏览器做「验证」。但这会留下绑定旧 loop 的 BrowserContext，下次再点「开始上传」会创建
    新 loop + 新线程，而 `_upload_async` 仍然会从 `active_contexts[name]` 拿到旧 ctx，
    导致 `Page.goto: The future belongs to a different loop` 等跨 loop 错误。

    现在改为：上传结束（或异常）即关 ctx + 释放 loop。验证若需要会自己 headless launch
    （`_verify_async` 已有 fallback）。
    """
    stop_evt = asyncio.Event()
    _browser_stop_events[account_name] = stop_evt
    with _browser_loop_lock:
        _browser_loops[account_name] = asyncio.get_running_loop()
    try:
        await _upload_async(account_name, csv_content, schedule_interval_min)
    finally:
        upload_state['running'] = False
        upload_state['account'] = None
        ctx = active_contexts.pop(account_name, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
            _cleanup_temp_profile(ctx, persist_back=False)
        if not stop_evt.is_set():
            stop_evt.set()


def _upload_background(account_name, csv_content, schedule_interval_min=None):
    """Background task: run the upload process and broadcast progress."""
    upload_state['running'] = True
    upload_state['abort'] = False
    upload_state['account'] = account_name

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_upload_session(account_name, csv_content, schedule_interval_min))
    except Exception as e:
        logger.error(f'Upload error: {e}')
        broadcast({'type': 'upload-end', 'success': False, 'error': str(e)})
    finally:
        with _browser_loop_lock:
            _browser_loops.pop(account_name, None)
        _browser_stop_events.pop(account_name, None)
        upload_state['running'] = False
        upload_state['account'] = None
        loop.close()


async def _upload_async(account_name, csv_content, schedule_interval_min=None):
    """Async upload process."""
    acct = getAccount(account_name)
    if acct is None:
        logger.error(f'账号不存在: {account_name}')
        broadcast({
            'type': 'upload-end',
            'success': False,
            'error': '账号不存在'
        })
        return

    # 防御：若上一次会话遗留了绑定旧 loop 的 ctx，丢弃后重建，避免跨 loop 报错。
    ctx = active_contexts.get(account_name)
    if ctx is not None:
        cur_loop = asyncio.get_running_loop()
        with _browser_loop_lock:
            bound_loop = _browser_loops.get(account_name)
        if bound_loop is not cur_loop:
            logger.warn(
                'Detected stale browser context bound to a different loop, '
                'discarding and creating a new one.'
            )
            active_contexts.pop(account_name, None)
            ctx = None

    upload_profile_dir = acct['profileDir']
    upload_profile_temp_root = None
    try:
        if ctx is None:
            await unlock_profile(acct['profileDir'])
            use_headless = upload_headless_from_env()
            logger.info(
                f'Upload browser: {"headless" if use_headless else "visible window"} '
                f'(HEADLESS_UPLOAD=0/false/off 才有界面；未设置则默认无头)'
            )
            if use_headless and sys.platform == 'darwin':
                upload_profile_dir, upload_profile_temp_root = _prepare_headless_upload_profile(
                    acct['profileDir']
                )
            ctx = await init_browser(upload_profile_dir, headless=use_headless)
            if upload_profile_temp_root:
                ctx._wx_temp_profile_root = upload_profile_temp_root
                ctx._wx_temp_profile_dir = upload_profile_dir
            active_contexts[account_name] = ctx

        if len(ctx.pages) == 0:
            logger.error(
                'Browser launched but no page created — '
                'profile may be locked or Chrome crashed'
            )
            broadcast({
                'type': 'upload-end',
                'success': False,
                'error': '浏览器启动失败，请检查是否有其他 Chrome 实例占用同一账号，或重启电脑后重试',
            })
            return

        # Parse CSV
        try:
            records = load_csv_from_string(csv_content)
        except Exception as e:
            logger.error(f'CSV parse error: {e}')
            broadcast({
                'type': 'upload-end',
                'success': False,
                'error': str(e),
            })
            return

        try:
            gi = (
                int(schedule_interval_min)
                if schedule_interval_min is not None
                else int(os.environ.get('SCHEDULE_INTERVAL_MIN', '1') or '1')
            )
        except (TypeError, ValueError):
            gi = 1
        if gi < 1:
            gi = 1
        if gi > 1440:
            gi = 1440
        for r in records:
            try:
                ex = int(r.get('schedule_interval_min', 0) or 0)
            except (TypeError, ValueError):
                ex = 0
            if ex <= 0:
                r['schedule_interval_min'] = gi

        records = preflight_records(records)
        valid_count = len([r for r in records if not r.get('_skip')])
        if valid_count == 0:
            logger.warn('No valid records')
            broadcast({
                'type': 'upload-end',
                'success': False,
                'error': 'No valid records',
            })
            return

        logger.info(
            f'Preflight: {valid_count} valid, {len(records) - valid_count} skipped')

        def on_progress(p):
            broadcast({
                'type': 'progress',
                'current': p['current'],
                'total': p['total'],
                'status': p['status'],
                'title': p['title'],
            })

        def on_login_expired(record):
            updateAccountStatus(account_name, 'needs-login')
            broadcast({
                'type': 'login-expired',
                'account': account_name,
                'title': record.get('title', ''),
            })

        results = await batch_upload(ctx, records, {
            'resume': False,
            'abortSignal': upload_state,
            'onProgress': on_progress,
            'onLoginExpired': on_login_expired,
        })

        login_expired = any(r.get('_loginExpired') for r in results)
        published_count = len([r for r in results if r.get('status') == 'published'])
        broadcast({
            'type': 'upload-end',
            'success': True,
            'results': published_count,
            'total': len(results),
            'loginExpired': login_expired,
        })
        logger.info(f'Upload complete: {published_count}/{len(results)}')

        # Cleanup old temp files (>36 hours), keep recent ones for "恢复上次"
        try:
            now_ms = time.time() * 1000
            for f in os.listdir(UPLOADS_DIR):
                fp = os.path.join(UPLOADS_DIR, f)
                try:
                    if now_ms - os.path.getmtime(fp) > 36 * 3600000:
                        os.unlink(fp)
                except Exception:
                    pass
        except Exception:
            pass
    finally:
        if upload_profile_temp_root and ctx is None:
            shutil.rmtree(upload_profile_temp_root, ignore_errors=True)


@app.route('/api/upload/start', methods=['POST'])
def api_upload_start():
    if upload_state['running']:
        return jsonify({'error': '当前已有上传任务正在进行'}), 400

    data = request.get_json(force=True)
    account_name = (data.get('account') or PRIMARY_ACCOUNT_NAME).strip()
    csv_content = data.get('csv', '')
    raw_iv = data.get('schedule_interval_min')
    try:
        schedule_iv = int(raw_iv) if raw_iv is not None else None
    except (TypeError, ValueError):
        schedule_iv = None

    if not ensure_primary_account_name(account_name):
        return jsonify({'error': '当前为单账号模式，仅支持主账号上传'}), 403
    if not csv_content:
        return jsonify({'error': '缺少 CSV 内容'}), 400

    acct = getAccount(account_name)
    if acct is None:
        return jsonify({'error': '账号不存在'}), 404

    # 开始上传前：若有打开中的账号浏览器，直接关闭/清理后再继续，不作为错误返回
    _close_account_browser_for_upload_start(account_name, acct)

    # Save for crash recovery
    with open(LAST_BATCH_PATH, 'w', encoding='utf-8') as f:
        f.write(csv_content)

    # Start background upload thread
    t = threading.Thread(
        target=_upload_background,
        args=(account_name, csv_content, schedule_iv),
        daemon=True,
    )
    t.start()

    return jsonify({'message': '上传任务已开始'})


@app.route('/api/upload/stop', methods=['POST'])
def api_upload_stop():
    """停止上传。
    - 默认（不带 force）：仅置 abort 标志；当前正在跑的视频会等到下一次轮询/检查点
      才退出，整体可能要等一阵（特别是 set_input_files 长超时阶段）。
    - force=true：在浏览器 loop 上立即关闭账号 context，让 Playwright 抛错，
      `_upload_async` 走 except 退出，立刻广播 upload-end，避免前端 UI 卡住。
    """
    force = False
    try:
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict) and data.get('force'):
            force = True
    except Exception:
        pass
    if request.args.get('force', '').lower() in ('1', 'true', 'yes'):
        force = True

    upload_state['abort'] = True
    if not force:
        return jsonify({'message': '将在当前步骤完成后停止'})

    name = upload_state.get('account')
    closed = False
    if name:
        ctx = active_contexts.get(name)
        with _browser_loop_lock:
            loop = _browser_loops.get(name)
        if ctx is not None and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    _async_close_account_browser(name), loop
                )
                closed = True
            except Exception as e:
                logger.warn(f'force stop: schedule close failed: {e}')

    # 提前 broadcast upload-end，让前端立即解锁；后台真正退出后还会再发一条（幂等）。
    broadcast({
        'type': 'upload-end',
        'success': False,
        'error': 'Force stopped by user',
        'forced': True,
    })
    upload_state['running'] = False
    return jsonify({
        'message': 'Force stopped',
        'browserClosed': closed,
    })


@app.route('/api/upload/state', methods=['GET'])
def api_upload_state():
    """供前端在「停止」之后做二次校准，避免 UI 与后端状态不一致。"""
    return jsonify({
        'running': bool(upload_state.get('running')),
        'abort': bool(upload_state.get('abort')),
        'account': upload_state.get('account'),
    })


@app.route('/api/upload/last-csv', methods=['GET'])
def api_upload_last_csv():
    try:
        with open(LAST_BATCH_PATH, 'r', encoding='utf-8') as f:
            csv_content = f.read()
        records = load_csv_from_string(csv_content)
        validated = preflight_records(records)
        entries = []
        for r in validated:
            entries.append({
                'video_path': r.get('video_path', ''),
                'cover_path': r.get('cover_path', '') or '',
                'title': r.get('title', '') or '',
                'description': r.get('description', '') or '',
                'short_drama_name': r.get('short_drama_name', '') or '',
                'publish_time': r.get('publish_time', '') or '',
                'valid': not r.get('_skip', False),
                'error': r.get('_skipReason', '') or '',
            })
        return jsonify({'entries': entries})
    except Exception:
        return jsonify({'entries': None})


@app.route('/api/upload/validate', methods=['POST'])
def api_upload_validate():
    try:
        data = request.get_json(force=True)
        csv_content = data.get('csv', '')
        if not csv_content:
            return jsonify({'error': '缺少 CSV 内容'}), 400
        records = load_csv_from_string(csv_content)
        validated = preflight_records(records)
        results = []
        for r in validated:
            results.append({
                'title': r.get('title', ''),
                'video_path': r.get('video_path', ''),
                'valid': not r.get('_skip', False),
                'error': r.get('_skipReason', '') or '',
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/update/check', methods=['GET'])
def api_update_check():
    try:
        return jsonify(resolve_update_info())
    except Exception as e:
        return jsonify({
            'enabled': False,
            'error': str(e),
            'current_version': load_local_version_info().get('version', '0.0.0'),
            'platform': get_platform_name(),
        }), 200


def _perform_update_download(info):
    version = str(info.get('latest_version') or 'latest')
    download_url = str(info.get('download_url') or '').strip()
    expected_sha = str(info.get('sha256') or '').strip().lower()
    set_update_state(
        running=True,
        status='running',
        stage='downloading',
        progress=0,
        indeterminate=False,
        message='正在下载更新包...',
        error='',
        version=version,
        path='',
        open_target='',
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at='',
            restart_ready=False,
            restarting=False,
    )

    try:
        def on_progress(downloaded, total, _filename):
            progress = int(downloaded * 100 / total) if total > 0 else 0
            set_update_state(
                running=True,
                status='running',
                stage='downloading',
                progress=progress,
                indeterminate=total <= 0,
                message='正在下载更新包...',
            )

        local_path = download_update_package(download_url, version, progress_callback=on_progress)
        set_update_state(
            running=True,
            status='running',
            stage='verifying',
            progress=100,
            indeterminate=True,
            message='正在校验更新包...',
            path=local_path,
        )

        actual_sha = ''
        if expected_sha:
            actual_sha = sha256_file(local_path).lower()
            if actual_sha != expected_sha:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                raise ValueError('更新包校验失败，请重新下载')

        set_update_state(
            running=True,
            status='running',
            stage='preparing',
            progress=100,
            indeterminate=True,
            message='正在准备新版本...',
        )
        prepared = prepare_downloaded_update(local_path, version)
        open_target = prepared.get('open_target') or local_path

        set_update_state(
            running=True,
            status='running',
            stage='opening',
            progress=100,
            indeterminate=True,
            message='正在打开新版本...',
            open_target=open_target,
        )
        open_downloaded_package(open_target)
        set_update_state(
            running=False,
            status='completed',
            stage='completed',
            progress=100,
            indeterminate=False,
            message='更新已准备完成，正在等待重启应用。',
            error='',
            path=local_path,
            open_target=open_target,
            finished_at=datetime.now(timezone.utc).isoformat(),
            restart_ready=True,
            restarting=False,
        )
    except Exception as e:
        set_update_state(
            running=False,
            status='failed',
            stage='failed',
            indeterminate=False,
            message='更新失败',
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            restart_ready=False,
            restarting=False,
        )


@app.route('/api/update/status', methods=['GET'])
def api_update_status():
    return jsonify(get_update_state())


@app.route('/api/update/restart', methods=['POST'])
def api_update_restart():
    try:
        state = get_update_state()
        open_target = str(state.get('open_target') or '').strip()
        if not open_target:
            return jsonify({'error': '新版本尚未准备完成'}), 400
        if state.get('restarting'):
            return jsonify({
                'message': '应用正在重启',
                'state': state,
            }), 202
        if not os.path.exists(open_target):
            return jsonify({'error': '未找到新版本启动文件'}), 400

        set_update_state(
            running=False,
            status='restarting',
            stage='restarting',
            progress=100,
            indeterminate=True,
            message='正在启动新版本并关闭当前应用...',
            error='',
            restart_ready=False,
            restarting=True,
        )
        threading.Thread(target=_restart_app_process, args=(open_target,), daemon=True).start()
        return jsonify({
            'message': '正在重启应用',
            'state': get_update_state(),
        }), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/update/download', methods=['POST'])
def api_update_download():
    try:
        state = get_update_state()
        if state.get('running'):
            return jsonify({
                'error': '已有更新任务正在进行中',
                'state': state,
            }), 409
        info = resolve_update_info()
        if not info.get('enabled'):
            return jsonify({'error': info.get('message') or '未配置更新地址'}), 400
        if not info.get('available'):
            return jsonify({'error': '当前已是最新版本'}), 400
        download_url = info.get('download_url')
        if not download_url:
            return jsonify({'error': '当前平台未配置下载地址'}), 400

        reset_update_state()
        t = threading.Thread(target=_perform_update_download, args=(info,), daemon=True)
        t.start()
        return jsonify({
            'message': '已开始下载更新',
            'state': get_update_state(),
            'version': info.get('latest_version'),
        }), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/status', methods=['GET'])
def api_upload_status():
    return jsonify({
        'running': upload_state['running'],
        'abort': upload_state['abort'],
    })


# ── File upload (drag-drop support) ──


@app.route('/api/upload/file', methods=['POST'])
def api_upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': '未检测到上传文件'}), 400
        f = request.files['file']
        if f.filename == '':
            return jsonify({'error': '未选择文件'}), 400

        original_name = f.filename
        # Generate safe filename: yyyymmddHHMMSS_original
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        safe_name = f'{ts}_{original_name}'
        dest = os.path.join(UPLOADS_DIR, safe_name)
        f.save(dest)
        return jsonify({
            'path': dest,
            'name': original_name,
            'size': os.path.getsize(dest),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/file', methods=['DELETE'])
def api_delete_upload_file():
    try:
        data = request.get_json(silent=True) or {}
        target_path = str(data.get('path') or '').strip()
        if not target_path:
            return jsonify({'error': '缺少文件路径'}), 400

        uploads_root = os.path.realpath(UPLOADS_DIR)
        real_target = os.path.realpath(target_path)
        try:
            common = os.path.commonpath([uploads_root, real_target])
        except ValueError:
            common = ''
        if common != uploads_root:
            return jsonify({'error': '仅允许删除 uploads 目录内的素材文件'}), 403

        if not os.path.exists(real_target):
            return jsonify({'message': '文件已不存在', 'deleted': False}), 200
        if not os.path.isfile(real_target):
            return jsonify({'error': '目标不是文件'}), 400

        os.remove(real_target)
        return jsonify({'message': '素材已删除', 'deleted': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Results & log ──


@app.route('/api/results', methods=['GET'])
def api_results():
    try:
        if not os.path.exists(RESULTS_PATH):
            return jsonify([])
        with open(RESULTS_PATH, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                rows.append({
                    'video_path': row.get('video_path', '') or '',
                    'title': row.get('title', '') or '',
                    'status': row.get('status', '') or '',
                    'error': row.get('error', '') or '',
                })
        return jsonify(rows)
    except Exception:
        return jsonify([])


@app.route('/api/log', methods=['GET'])
def api_log():
    try:
        if not os.path.exists(LOG_PATH):
            return jsonify([])
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            text = f.read()
        lines = text.split('\n')
        lines = [l for l in lines if l.strip()]
        return jsonify(lines[-200:])
    except Exception:
        return jsonify([])


# ── Cache ──


@app.route('/api/cache/clear', methods=['POST'])
def api_cache_clear():
    try:
        cleared = 0
        if os.path.isdir(UPLOADS_DIR):
            for name in os.listdir(UPLOADS_DIR):
                fp = os.path.join(UPLOADS_DIR, name)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                        cleared += 1
                    elif os.path.isdir(fp):
                        shutil.rmtree(fp, ignore_errors=True)
                        cleared += 1
                except Exception:
                    pass
        return jsonify({'message': '缓存已清理', 'cleared': cleared})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Static files ──


@app.route('/favicon.ico')
def favicon():
    # Serve app.ico from RES_DIR or BASE_DIR to make Chrome app window look like a desktop app
    if os.path.exists(os.path.join(RES_DIR, 'app.ico')):
        return send_from_directory(RES_DIR, 'app.ico', mimetype='image/vnd.microsoft.icon')
    elif os.path.exists(os.path.join(BASE_DIR, 'app.ico')):
        return send_from_directory(BASE_DIR, 'app.ico', mimetype='image/vnd.microsoft.icon')
    # Fallback to empty response if no icon exists
    return '', 204


@app.route('/')
def index():
    return send_from_directory(os.path.join(RES_DIR, 'public'), 'index.html')


@app.route('/<path:path>')
def static_files(path):
    public_dir = os.path.join(RES_DIR, 'public')
    public_path = os.path.join(public_dir, path)
    if os.path.exists(public_path):
        return send_from_directory(public_dir, path)
    return send_from_directory(public_dir, 'index.html')


@app.route('/api/version')
def api_version():
    try:
        path = os.path.join(RES_DIR, 'version.json')
        if not os.path.exists(path):
            path = os.path.join(BASE_DIR, 'version.json')
        with open(path, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({'version': '0.0.0'})


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

if __name__ == '__main__':
    logger.info('Server starting...')
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('screenshots', exist_ok=True)

    print(f'Server: http://localhost:{PORT}')
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
