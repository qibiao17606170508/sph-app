#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_upload.py - 视频号批量上传工具 (Python Playwright 版)
Translated from batch-upload.js
"""

import asyncio
import csv
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from shutil import which as shutil_which

from playwright.async_api import async_playwright

__all__ = [
    'PROFILE_DIR', 'LOG_PATH', 'RESULTS_PATH', 'SCREENSHOTS_DIR', 'PLATFORM', 'MAX_RETRIES',
    'init_browser', 'unlock_profile', 'login_flow', 'batch_upload', 'process_video',
    'preflight_records', 'load_csv', 'load_csv_from_string', 'validate_title',
    'write_results', 'load_published_titles',
    'classify_error', 'is_login', 'detect_login_state', 'wait_for_upload_with_progress', 'select_short_drama',
    'set_cover', 'hide_location', 'verify_publish', 'wait_until', 'handle_login_expired',
    'set_schedule_publish',
    'probe_video', 'logger', 'notify_user',
    'upload_headless_from_env',
    'info_should_show_in_live_ui',
]

# ── Constants ──
_BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(_BASE_DIR, 'browser-profile')
LOG_PATH = os.path.join(_BASE_DIR, 'upload.log')
SCREENSHOTS_DIR = os.path.join(_BASE_DIR, 'screenshots')
RESULTS_PATH = os.path.join(_BASE_DIR, 'results.csv')
MAX_RETRIES = 2
# Playwright 硬限制：buffer 型 FilePayload 不得超过约 50MB（再大须走路径或 FileChooser）
_PLAYWRIGHT_FILE_PAYLOAD_MAX = 50 * 1024 * 1024

PLATFORM = {
    'maxFileSize': 20 * 1024 * 1024 * 1024,
    'maxDuration': 8 * 3600,
    'minDuration': 5,
    'allowedCodec': 'h264',
    'maxBitrate': 10 * 1000 * 1000,
    'allowedFormats': ['.mp4'],
    'titleMinLen': 6,
}


# ── Logger ──
# 控制台 + upload.log：始终写入完整 INFO（便于排错）。
# Web 端「实时日志」：由 server.py 在 WebSocket 广播前调用 info_should_show_in_live_ui() 再过滤；
# 设置 VERBOSE_LOG=1 则实时日志也显示全部 INFO。
def _verbose_log_enabled():
    return os.environ.get('VERBOSE_LOG', '').strip().lower() in ('1', 'true', 'yes', 'on')


# 仅用于 Socket.IO 实时日志白名单（与终端/文件无关）
_LIVE_UI_INFO_SUBSTR = (
    '=== 发表视频',
    '上传进度 ',
    '检测到上传进度',
    '选择剧集：',
    '定时发表：',
    '发布成功',
    'Upload complete',  # 单条上传结束 + 批次 Upload complete: x/x
)


def info_should_show_in_live_ui(msg) -> bool:
    """是否把该条 INFO 推到前端实时日志。VERBOSE_LOG=1 时全部放行。"""
    if _verbose_log_enabled():
        return True
    text = msg if isinstance(msg, str) else str(msg)
    return any(s in text for s in _LIVE_UI_INFO_SUBSTR)


class _Logger:
    """Logger：控制台与 upload.log 全量；Web 实时日志由 server 侧按 info_should_show_in_live_ui 过滤。"""

    def _write(self, level, msg):
        ts = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
        line = f'[{ts}] [{level}] {msg}'
        print(line)
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass

    def info(self, msg):
        self._write('INFO', msg)

    def warn(self, msg):
        self._write('WARN', msg)

    def error(self, msg):
        self._write('ERROR', msg)


logger = _Logger()


def _windows_hidden_subprocess_kwargs():
    if sys.platform != 'win32':
        return {}
    kwargs = {}
    create_no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if create_no_window:
        kwargs['creationflags'] = create_no_window
    startupinfo_cls = getattr(subprocess, 'STARTUPINFO', None)
    startf_use_show_window = getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= startf_use_show_window
        kwargs['startupinfo'] = startupinfo
    return kwargs


# ── Desktop notification ──
def notify_user(title, message):
    """跨平台桌面通知（best-effort）。
    - macOS: osascript display notification
    - Windows: PowerShell MessageBox
    - 其他: 仅写日志
    """
    try:
        if sys.platform == 'darwin':
            s = (message or '').replace('\\', '\\\\').replace('"', '\\"')
            t = (title or '').replace('\\', '\\\\').replace('"', '\\"')
            subprocess.run(
                ['osascript', '-e',
                 f'display notification "{s}" with title "{t}"'],
                timeout=5, capture_output=True
            )
        elif sys.platform.startswith('win'):
            s = (message or '').replace("'", "''").replace('"', '``')
            t = (title or '').replace("'", "''")
            subprocess.run(
                f'powershell -Command "Add-Type -AssemblyName System.Windows.Forms; '
                f'[System.Windows.Forms.MessageBox]::Show(\'{s}\', \'{t}\')"',
                shell=True, timeout=10, **_windows_hidden_subprocess_kwargs()
            )
    except Exception:
        pass


# ── Internal helpers ──
def _check_abort(abort_signal):
    """Check if abort signal is set, supporting both dict and object-style signals."""
    if abort_signal is None:
        return False
    if isinstance(abort_signal, dict):
        return abort_signal.get('abort', False)
    return bool(getattr(abort_signal, 'abort', False))


async def _async_sleep_interruptible(total_sec: float, abort_signal) -> bool:
    """分段 sleep，便于上传中止。返回 False 表示等待期间已中止。"""
    if total_sec <= 0:
        return True
    remaining = float(total_sec)
    while remaining > 0:
        if _check_abort(abort_signal):
            return False
        step = min(15.0, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return True


def _safe_filename(text, max_len=30):
    """Replace unsafe path characters and truncate."""
    s = str(text) if text is not None else ''
    return re.sub(r'[<>:"/\\|?*]', '_', s)[:max_len]


# ── CSV helpers ──
def csv_escape(val):
    """Escape a value for CSV output."""
    s = str(val) if val is not None else ''
    if ',' in s or '"' in s or '\n' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def write_results(results, results_path=None):
    """Write results array to CSV with BOM."""
    rp = results_path or RESULTS_PATH
    header = 'video_path,title,status,error'
    rows = [header]
    for r in results:
        rows.append(','.join([
            csv_escape(r.get('video_path', '')),
            csv_escape(r.get('title', '')),
            csv_escape(r.get('status', '')),
            csv_escape(r.get('error', '')),
        ]))
    with open(rp, 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(rows))


def load_published_titles(results_path, resume):
    """Load previously published titles from results CSV (for --resume)."""
    published = set()
    if not resume or not os.path.exists(results_path):
        return published
    with open(results_path, 'r', encoding='utf-8-sig') as f:
        text = f.read()
    lines = text.split('\n')[1:]  # skip header
    for line in lines:
        if not line.strip():
            continue
        try:
            reader = csv.DictReader(io.StringIO(line), fieldnames=['vp', 't', 'st', 'err'])
            for row in reader:
                if row.get('st') == 'published':
                    published.add(row.get('t', ''))
        except Exception:
            pass
    return published


# ── ffprobe ──
def probe_video(file_path):
    """Use ffprobe to get video metadata."""
    try:
        if not shutil_which('ffprobe'):
            return None
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_format', '-show_streams', file_path],
            capture_output=True, text=True, timeout=15
        )
        if not result.stdout:
            return None
        data = json.loads(result.stdout)
        streams = data.get('streams', []) or []
        vs = None
        for s in streams:
            if s.get('codec_type') == 'video':
                vs = s
                break
        fmt = data.get('format', {}) or {}
        return {
            'duration': float(fmt.get('duration', 0)),
            'size': int(fmt.get('size', 0)),
            'codec': vs.get('codec_name', 'unknown') if vs else 'unknown',
            'bitrate': int(fmt.get('bit_rate', 0)),
            'width': vs.get('width', 0) if vs else 0,
            'height': vs.get('height', 0) if vs else 0,
        }
    except Exception as e:
        logger.warn(f'  ffprobe failed for {os.path.basename(file_path)}: {e}')
        return None


# ── Validation ──
def load_csv(csv_path):
    """Load and parse a CSV file, return list of dicts."""
    if not os.path.exists(csv_path):
        raise Exception(f'CSV not found: {csv_path}')
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        return [row for row in reader]


def load_csv_from_string(csv_content):
    """Parse CSV from a string, return list of dicts."""
    reader = csv.DictReader(io.StringIO(csv_content), skipinitialspace=True)
    return [row for row in reader]


def validate_title(title):
    """Validate title: allowed chars and minimum length."""
    if not title:
        return None  # optional field
    allowed_basic = set(
        'abcdefghijklmnopqrstuvwxyz'
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        '0123456789 《》（）《》“”‘’：+？%℃ '
    )
    for ch in title:
        if ch not in allowed_basic and not ('一' <= ch <= '鿿'):
            return f'Unsupported char "{ch}"'
    if len(title) < 6:
        return f'Title too short ({len(title)}), min 6'
    return None


def preflight_records(records):
    """Run pre-flight checks on all records, mark invalid ones with _skip."""
    for r in records:
        errs = []

        video_path_val = r.get('video_path') or ''
        vp = video_path_val.strip()
        if not vp:
            errs.append('Missing video_path')
        else:
            if not os.path.exists(vp):
                errs.append(f'File not found: {vp}')
            else:
                stat = os.stat(vp)
                if stat.st_size > PLATFORM['maxFileSize']:
                    size_gb = stat.st_size / 1024 / 1024 / 1024
                    errs.append(f'File too large ({size_gb:.1f} GB)')
                ext = os.path.splitext(vp)[1].lower()
                if ext not in PLATFORM['allowedFormats']:
                    logger.warn(
                        f'  [Preflight] {r.get("title", "")}: format {ext} — '
                        f'官方页可传多种格式；本工具走自动化，MP4(H.264) 一般最省事'
                    )
                info = probe_video(vp)
                if info:
                    if info['duration'] > PLATFORM['maxDuration']:
                        hours = info['duration'] / 3600
                        errs.append(f'Video too long ({hours:.1f}h)')
                    if info['codec'] != PLATFORM['allowedCodec'] and info['codec'] != 'unknown':
                        logger.warn(f'  [Preflight] {r.get("title", "")}: codec {info["codec"]}')
                    if info['bitrate'] > PLATFORM['maxBitrate']:
                        mbps = info['bitrate'] / 1000 / 1000
                        logger.warn(f'  [Preflight] {r.get("title", "")}: bitrate {mbps:.1f} Mbps')

        title_val = r.get('title') or ''
        if title_val.strip():
            ve = validate_title(title_val.strip())
            if ve:
                errs.append(ve)

        if errs:
            r['_skip'] = True
            r['_skipReason'] = '; '.join(errs)
    return records


# ── Error classification ──
def classify_error(msg):
    """Classify error message into error type for retry/abort decisions."""
    if not msg:
        return 'fatal'
    low = msg.lower()
    if 'no files attached' in low or 'not accepted by any file input' in low:
        return 'upload-failed'
    login_keywords = ['login', 'Login', '未登录', '登录']
    for k in login_keywords:
        if k in msg:
            return 'login-expired'
    title_keywords = ['title', 'Title']
    for k in title_keywords:
        if k in msg:
            return 'title-error'
    retry_keywords = [
        'timeout', 'Timeout', 'net::ERR_', 'ETIMEDOUT',
        'ECONNRESET', 'NS_ERROR_', 'CONNECTION', 'INTERNET_',
    ]
    for k in retry_keywords:
        if k in msg:
            return 'retryable'
    return 'fatal'


def is_login(url):
    """Check if URL indicates a login page."""
    return 'login' in url


_STRONG_LOGGED_OUT_TEXTS = (
    '扫码登录',
    '微信扫码登录',
    '请使用微信扫码',
    '请在手机上确认登录',
    '确认登录',
    '二维码已失效',
    '二维码已过期',
    '二维码过期',
    '重新获取二维码',
    '重新获取',
    '请先登录',
    '登录后即可上传视频',
    '登录后可上传视频',
    '账号异常',
    '帐号异常',
)
_WEAK_LOGGED_OUT_TEXTS = (
    '未登录',
    '重新登录',
    '登录失效',
    '登录态失效',
)
_LOGGED_IN_TEXTS = (
    '内容管理',
    '发表视频',
    '直播管理',
    '数据中心',
    '数据助手',
    '评论管理',
    '创作者中心',
    '收益数据',
    '视频号助手',
    '首页',
    '动态',
    '私信',
    '设置',
)
_LOGGED_IN_SELECTORS = (
    'input[type=file]',
    '.input-editor',
    '.weui-desktop-layout',
    '.weui-desktop-side',
    '.weui-desktop-layout__main',
    '.menu-list',
    '.channels-header',
    '.avatar',
)


def _normalize_page_text(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def _find_text_matches(text, keywords):
    return [kw for kw in keywords if kw and kw in text]


async def _page_has_any_selector(page, selectors):
    for selector in selectors:
        try:
            if await page.locator(selector).first.count() > 0:
                return selector
        except Exception:
            pass
    return ''


async def detect_login_state(page):
    """综合 URL、页面文案和后台元素判断当前是否仍处于登录状态。"""
    current_url = page.url or ''
    normalized_url = current_url.lower()
    body_text = ''
    try:
        body_text = _normalize_page_text(await page.text_content('body') or '')
    except Exception:
        pass

    strong_logged_out = _find_text_matches(body_text, _STRONG_LOGGED_OUT_TEXTS)
    weak_logged_out = _find_text_matches(body_text, _WEAK_LOGGED_OUT_TEXTS)
    logged_in_texts = _find_text_matches(body_text, _LOGGED_IN_TEXTS)
    matched_selector = await _page_has_any_selector(page, _LOGGED_IN_SELECTORS)

    # 1. 如果明确出现“扫码登录”等强烈的未登录特征词，优先判断为未登录
    if strong_logged_out:
        return {'logged_in': False, 'reason': 'login_text', 'detail': strong_logged_out[0]}
    
    # 2. 如果 URL 是明确的 login 页面，判断为未登录
    if is_login(current_url):
        return {'logged_in': False, 'reason': 'login_url', 'detail': current_url}
        
    # 3. 如果出现了弱未登录词且没有任何已登录特征词，判断为未登录
    if weak_logged_out and not logged_in_texts and not matched_selector:
        return {'logged_in': False, 'reason': 'login_text_weak', 'detail': weak_logged_out[0]}

    # 4. 如果找到了已登录的特征选择器（最可靠），判断为已登录
    if matched_selector:
        return {'logged_in': True, 'reason': 'app_selector', 'detail': matched_selector}
        
    # 5. 如果在 platform 路由下且找到了已登录的特征词
    if '/platform' in normalized_url and logged_in_texts:
        return {'logged_in': True, 'reason': 'app_text', 'detail': logged_in_texts[0]}
        
    # 6. 如果没有明确在 platform 下，但找到了多个已登录特征词
    if len(logged_in_texts) >= 2:
        return {'logged_in': True, 'reason': 'app_text', 'detail': logged_in_texts[0]}
        
    # 7. 如果在 platform 路由下，且没有任何未登录特征，默认认为是登录状态
    if '/platform' in normalized_url and not strong_logged_out and not weak_logged_out:
        return {'logged_in': True, 'reason': 'platform_url', 'detail': current_url}
        
    # 8. 最后的回退策略：如果没有明确被识别为已登录，统统认为未登录，避免误判为“有效”
    return {'logged_in': False, 'reason': 'fallback', 'detail': current_url}


def upload_headless_from_env():
    """批量上传是否用无头 Chromium。默认有界面；仅 HEADLESS_UPLOAD=1/true/yes/on 才无头。"""
    raw = os.environ.get('HEADLESS_UPLOAD', '').strip().lower()
    if not raw:
        return False
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    return False


def _find_chromium_executable(headless=False):
    """Find the best Chromium executable to use.
    Priority:
    1. System Google Chrome (most stable, users already have it for UI)
    2. Bundled Playwright Chromium (fallback)
    """
    # 1. Check system Google Chrome
    candidates = []
    if sys.platform == 'win32':
        for env_name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            root = os.environ.get(env_name, '').strip()
            if root:
                candidates.append(os.path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    elif sys.platform == 'darwin':
        candidates += [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
    else:
        candidates.append(shutil_which('google-chrome'))
        candidates.append(shutil_which('chrome'))
        
    for path in candidates:
        if path and os.path.isfile(path):
            # Windows 下检查是否有执行权限，避免 spawn EPERM
            if sys.platform == 'win32':
                try:
                    # 尝试用 os.access 检查 X_OK
                    if not os.access(path, os.X_OK):
                        continue
                except Exception:
                    pass
            logger.info(f"  [Browser] Found system Chrome: {path}")
            return path
            
    # 2. Check bundled Playwright Chromium
    pw_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')
    if pw_path and os.path.isdir(pw_path):
        import glob
        if headless and sys.platform == 'win32':
            dirs = glob.glob(os.path.join(pw_path, 'chromium_headless_shell-*'))
            if dirs:
                dirs.sort()
                exe = os.path.join(dirs[-1], 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')
                if os.path.isfile(exe):
                    logger.info(f"  [Browser] Found bundled headless shell: {exe}")
                    return exe
                    
        dirs = glob.glob(os.path.join(pw_path, 'chromium-*'))
        if dirs:
            dirs.sort()
            latest = dirs[-1]
            if sys.platform == 'win32':
                exe = os.path.join(latest, 'chrome-win', 'chrome.exe')
            elif sys.platform == 'darwin':
                exe = os.path.join(latest, 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium')
            else:
                exe = os.path.join(latest, 'chrome-linux', 'chrome')
                
            if os.path.isfile(exe):
                logger.info(f"  [Browser] Found bundled Chromium: {exe}")
                return exe
                
    return None


# ── Browser helpers ──
def _bind_context_close_with_playwright(context, playwright_driver):
    """确保关闭 BrowserContext 时，同时停止 async_playwright() 的底层连接。

    Windows + Python 3.13 下，如果只 close context 而不 stop playwright，
    很容易在事件循环退出时看到:
    - Task was destroyed but it is pending
    - unclosed transport / closed pipe
    """
    orig_close = context.close
    if getattr(context, '_wx_close_wrapped', False):
        return context

    async def _close_and_stop(*args, **kwargs):
        close_err = None
        try:
            return await orig_close(*args, **kwargs)
        except Exception as e:
            close_err = e
        finally:
            try:
                await playwright_driver.stop()
            except Exception:
                pass
        if close_err is not None:
            raise close_err

    context.close = _close_and_stop
    context._wx_close_wrapped = True
    context._wx_playwright_driver = playwright_driver
    return context


async def init_browser(profile_dir, headless=True):
    """Launch a persistent Chromium browser context with the given profile."""
    _pw_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '')
    if _pw_path and os.path.isdir(_pw_path):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', _pw_path)
    p = await async_playwright().start()
    args = [
        '--disable-infobars',
        '--disable-extensions',
    ]
    
    # 针对 Linux 和 Windows 的特定优化
    if sys.platform == 'linux':
        args += [
            '--disable-gpu',
            '--no-sandbox',  # Linux 环境下（尤其是 Docker/Root 运行）通常需要此参数
            '--disable-dev-shm-usage',
        ]
    elif sys.platform == 'win32':
        args += [
            '--disable-gpu',
            '--disable-dev-shm-usage',
        ]
        # 注意：移除了 Windows 和 macOS 下默认的 --no-sandbox
        # 因为在常规桌面环境下不需要，且会触发“不受支持的命令行标记”的黄条警告
    
    # 强制清理可能导致崩溃的锁定目录
    try:
        if os.path.exists(profile_dir):
            # 暴力尝试修复整个目录权限
            if sys.platform == 'darwin':
                subprocess.run(['chmod', '-R', '777', profile_dir], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    # 保持页面按真实窗口渲染（no_viewport=True）的同时，让有界面模式窗口最大化（不进全屏）
    if not headless:
        args += ['--start-maximized']
        
    launch_kwargs = {
        'user_data_dir': profile_dir,
        'headless': headless,
        'args': args,
        # 禁用 Playwright 视口模拟，按真实浏览器窗口宽度渲染页面
        'no_viewport': True,
        'locale': 'zh-CN',
        'ignore_default_args': ['--enable-automation'],
    }
    
    exe_path = _find_chromium_executable(headless)
    if exe_path:
        launch_kwargs['executable_path'] = exe_path
    else:
        # 本地 macOS 开发时优先使用系统 Chrome，避免 Playwright 自带 Chromium 在本机直接崩溃
        if sys.platform == 'darwin' and not getattr(sys, 'frozen', False):
            launch_kwargs['channel'] = 'chrome'

    try:
        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        return _bind_context_close_with_playwright(context, p)
    except Exception as e:
        err_msg = str(e)
        # 处理 ProcessSingleton 锁定问题 (Error code 32)
        if 'ProcessSingleton' in err_msg or 'SingletonLock' in err_msg:
            logger.warn(f"  [Browser] Launch failed due to ProcessSingleton lock. Retrying cleanup...")
            await unlock_profile(profile_dir)
            await asyncio.sleep(1)
            try:
                context = await p.chromium.launch_persistent_context(**launch_kwargs)
                logger.info("  [Browser] Retry launch successful after secondary cleanup.")
                return _bind_context_close_with_playwright(context, p)
            except Exception as e_retry:
                logger.error(f"  [Browser] Retry launch still failed: {e_retry}")
                # 继续向下执行权限错误处理
                err_msg = str(e_retry)
                e = e_retry

        if 'EPERM' in err_msg or 'Access is denied' in err_msg or 'permission denied' in err_msg:
            logger.warn(f"  [Browser] Launch failed with permission error: {e}. Trying fallback...")
            # 如果是因为系统 Chrome 权限问题，尝试清除 executable_path 让 Playwright 用自带的
            if 'executable_path' in launch_kwargs:
                launch_kwargs.pop('executable_path')
                # 尝试用自带的 chromium channel
                if sys.platform == 'darwin':
                    launch_kwargs['channel'] = 'chromium'
                try:
                    context = await p.chromium.launch_persistent_context(**launch_kwargs)
                    logger.info("  [Browser] Fallback launch successful.")
                    return _bind_context_close_with_playwright(context, p)
                except Exception as e2:
                    logger.error(f"  [Browser] Fallback launch also failed: {e2}")
                    try:
                        await p.stop()
                    except Exception:
                        pass
                    raise e2
        try:
            await p.stop()
        except Exception:
            pass
        raise e


async def unlock_profile(profile_dir):
    """Remove Chromium singleton lock files and kill residual processes if present."""
    # 1. 尝试清理残留进程
    import subprocess
    dir_name = os.path.basename(profile_dir)
    abs_profile_dir = os.path.abspath(profile_dir).lower()
    
    if sys.platform != 'win32':
        try:
            subprocess.run(['pkill', '-f', f'.*{dir_name}.*'], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    else:
        # Windows 增强清理逻辑：找到所有启动参数包含 profile_dir 的进程并结束
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline')
                    if not cmdline:
                        continue
                    # 检查命令行参数中是否包含 profile_dir
                    cmd_str = ' '.join(cmdline).lower()
                    if abs_profile_dir in cmd_str or dir_name.lower() in cmd_str:
                        pname = (proc.info.get('name') or '').lower()
                        if 'chrome' in pname or 'chromium' in pname or 'playwright' in pname:
                            logger.info(f"  [Cleanup] Killing residual browser process (PID: {proc.info['pid']}, Name: {proc.info['name']})")
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except ImportError:
            # 如果没装 psutil，退回到 wmic 暴力清理（针对常见浏览器名）
            try:
                for img in ['chrome.exe', 'chromium.exe', 'chrome-headless-shell.exe']:
                    subprocess.run(
                        ['taskkill', '/F', '/IM', img, '/T'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **_windows_hidden_subprocess_kwargs(),
                    )
            except Exception:
                pass

    # 2. 删除锁文件
    lock_files = [
        'SingletonLock', 'SingletonCookie', 'SingletonSocket',
        'LOCK', '.lock'
    ]
    
    dirs_to_check = [profile_dir]
    # 某些版本的 Chrome 会把锁文件放在 Default 目录下
    if os.path.exists(os.path.join(profile_dir, 'Default')):
        dirs_to_check.append(os.path.join(profile_dir, 'Default'))
        
    for d in dirs_to_check:
        if not os.path.isdir(d):
            continue
        
        # 尝试修复目录权限
        if sys.platform == 'win32':
            try:
                import stat
                os.chmod(d, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except Exception:
                pass

        for fname in lock_files:
            fp = os.path.join(d, fname)
            if os.path.lexists(fp):
                try:
                    # Windows 下尝试解除只读并强行删除
                    if sys.platform == 'win32':
                        import stat
                        try:
                            os.chmod(fp, stat.S_IWRITE)
                        except Exception:
                            pass
                    
                    if os.path.islink(fp):
                        os.unlink(fp)
                    else:
                        os.remove(fp)
                    logger.info(f"  [Cleanup] Removed lock file: {fp}")
                except OSError as e:
                    # 如果还是报错，说明可能被系统其它进程占用，记录警告
                    logger.warn(f'  [Cleanup] Could not remove {fp} (likely in use): {e}')
                    
    # 3. 清理 macOS 特有的缓存/锁定
    if sys.platform == 'darwin':
        cache_path = os.path.join(profile_dir, 'Default', 'Cache')
        if os.path.isdir(cache_path):
            try:
                shutil.rmtree(cache_path, ignore_errors=True)
            except Exception:
                pass


async def login_flow(browser_context):
    """Open login page, wait for user to scan QR code and press Enter."""
    page = browser_context.pages[0]
    await page.goto('https://channels.weixin.qq.com/', wait_until='domcontentloaded')
    logger.info('=== Scan QR code to login, then press Enter ===')
    loop = asyncio.get_event_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline),
            timeout=300
        )
    except asyncio.TimeoutError:
        pass
    logger.info('Login saved')


# ── Upload helpers ──
async def _extract_upload_percent(page):
    """优先从 .upload-progress 的 ant-progress-text 直接读上传百分比（含小数）；
    后备：扫所有 frame 的 \\d+% 文案，取靠近 progress/upload/percent 容器的那个。"""
    # 1) 直接读 ant-progress（视频号实测使用）
    for fr in page.frames:
        try:
            v = await fr.evaluate(
                """() => {
                    const sels = [
                        '.upload-progress .ant-progress-text',
                        '[class*="upload-progress"] [class*="progress-text"]',
                        '[class*="progress-text"]',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const raw = (el.title || el.textContent || '').trim();
                        const m = raw.match(/(\\d+(?:\\.\\d+)?)\\s*%/);
                        if (!m) continue;
                        const x = parseFloat(m[1]);
                        if (x >= 0 && x <= 100) return x;
                    }
                    return null;
                }"""
            )
            if v is not None:
                return v
        except Exception:
            pass
    # 2) 后备：扫文案
    candidates: list[tuple[int, float]] = []
    for fr in page.frames:
        try:
            loc = fr.get_by_text(re.compile(r'\d+(?:\.\d+)?%'))
            cnt = await loc.count()
        except Exception:
            cnt = 0
        for i in range(min(cnt, 40)):
            try:
                el = loc.nth(i)
                raw = (await el.inner_text()) or ''
            except Exception:
                continue
            m = re.search(r'(\d{1,3}(?:\.\d+)?)%', raw)
            if not m:
                continue
            v = float(m.group(1))
            if not (0 <= v <= 100):
                continue
            try:
                near_upload = await el.evaluate(
                    '''el => {
                        let p = el; let depth = 0;
                        while (p && depth < 6) {
                            const c = (p.className && p.className.toString()) || '';
                            if (/progress|upload|percent|loading/i.test(c)) return true;
                            p = p.parentElement; depth++;
                        }
                        return false;
                    }'''
                )
            except Exception:
                near_upload = False
            candidates.append((1 if near_upload else 0, v))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[0])
    return candidates[0][1]


async def _wx_upload_cover_ready(page) -> tuple[bool, str]:
    """
    视频号发表页：分片上传结束后往往**没有**「上传成功」类文案，但会生成封面图或预览 video。
    用于结束 wait_for_upload_with_progress，避免在 98% + 无新上行时一直空等。
    仅在媒体区域内、且 URL 像微信 CDN 的图/视频上判定，减少误报。
    """
    js = r"""() => {
        function visible(el) {
            if (!el || !el.isConnected) return null;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return null;
            if (parseFloat(s.opacity || '1') < 0.05) return null;
            const r = el.getBoundingClientRect();
            if (r.width < 36 || r.height < 24) return null;
            return r;
        }
        const hostSel = [
            '[class*="media-status-body"]', '[class*="media-status"]',
            '[class*="video-upload"]', '[class*="post-editor"]',
            '[class*="editor-content"]', '[class*="media-card"]',
            '[class*="material-list"]', '[class*="cover-wrap"]',
            '[class*="video-preview"]', '[class*="thumb"]',
        ].join(',');
        let hosts = document.querySelectorAll(hostSel);
        let looseImgMode = false;
        if (!hosts.length) {
            hosts = document.querySelectorAll(
                '[class*="media"] img[src^="http"], main img[src*="qpic"], main img[src*="mmbiz"]'
            );
            looseImgMode = true;
        }
        const cdnRe = /qpic\\.cn|qlogo\\.cn|mmbiz|gtimg|myqcloud|vod-qcloud|tc\\.qq|wxapp\\.tc|channels\\.cdn|weixin\\.qq\\.com|file\\.wx\\.qq|wx\\.qq/i;
        function checkImg(img) {
            if (!visible(img)) return false;
            if (!img.complete) return false;
            if (img.naturalWidth < 48 || img.naturalHeight < 28) return false;
            let u = (img.currentSrc || img.src || '').trim();
            if (u.startsWith('//')) u = 'https:' + u;
            if (!/^https?:\\/\\//i.test(u)) return false;
            if (!cdnRe.test(u)) return false;
            return true;
        }
        for (const host of hosts) {
            if (looseImgMode && host.tagName === 'IMG') {
                if (checkImg(host))
                    return { ok: true, reason: 'cover-img', hint: (host.currentSrc || host.src || '').slice(0, 96) };
                continue;
            }
            for (const img of host.querySelectorAll('img[src]')) {
                if (!checkImg(img)) continue;
                let u = (img.currentSrc || img.src || '').trim();
                if (u.startsWith('//')) u = 'https:' + u;
                return { ok: true, reason: 'cover-img', hint: u.slice(0, 96) };
            }
            for (const v of host.querySelectorAll('video')) {
                if (!visible(v)) continue;
                let src = (v.currentSrc || v.getAttribute('src') || '').trim();
                if (src.startsWith('//')) src = 'https:' + src;
                if (src.length < 16) continue;
                const r = v.getBoundingClientRect();
                if (r.width < 72 || r.height < 40) continue;
                if (/^blob:/i.test(src) || cdnRe.test(src))
                    return { ok: true, reason: 'preview-video', hint: src.slice(0, 96) };
            }
        }
        return { ok: false };
    }"""
    for fr in page.frames:
        try:
            r = await fr.evaluate(js)
        except Exception:
            r = None
        if r and r.get('ok'):
            return True, f'{r.get("reason", "")}:{r.get("hint", "")}'
    # Playwright 兜底（部分布局下 evaluate 选区未命中）
    for fr in page.frames:
        for sel in (
            '[class*="media-status"] img[src^="http"]',
            '[class*="media-status-body"] img[src^="http"]',
            'img[src*="qpic.cn"]',
            'img[src*="mmbiz.qpic"]',
            'img[src*="wx.qlogo"]',
        ):
            try:
                loc = fr.locator(sel).first
                if await loc.count() < 1:
                    continue
                if not await loc.is_visible():
                    continue
                box = await loc.bounding_box()
                if not box or box.get('width', 0) < 40 or box.get('height', 0) < 28:
                    continue
                nw = await loc.evaluate('e => e.naturalWidth || 0')
                nh = await loc.evaluate('e => e.naturalHeight || 0')
                if nw < 48 or nh < 28:
                    continue
                src = (await loc.get_attribute('src')) or ''
                return True, f'pw:{sel}:{src[:80]}'
            except Exception:
                continue
    return False, ''


_UPLOAD_DIAG_JS = r"""
if (!window.__wxDiag) {
  window.__wxDiag = {
    upRequests: 0, upBytes: 0, upLastSentAt: 0,
    upDoneOk: 0, upDoneFail: 0, upInflight: 0,
    upErrors: [], readErrors: [], lastConsoleErr: ''
  };
  const isUp = u => /upload|stream|chunk|qcloud|file\.video|video\.tc|wxapp\.tc|video\.qq|wxupload/i.test(u || '');
  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__wxU = u; this.__wxM = m; return _open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(body){
    try {
      if (isUp(this.__wxU)) {
        const sz = (body && (body.size || body.byteLength || (typeof body === 'string' ? body.length : 0))) || 0;
        window.__wxDiag.upRequests++;
        window.__wxDiag.upBytes += sz;
        window.__wxDiag.upLastSentAt = Date.now();
        window.__wxDiag.upInflight++;
        const onEnd = (kind) => {
          window.__wxDiag.upInflight = Math.max(0, window.__wxDiag.upInflight - 1);
          if (kind === 'load' && this.status >= 200 && this.status < 300) window.__wxDiag.upDoneOk++;
          else {
            window.__wxDiag.upDoneFail++;
            window.__wxDiag.upErrors.push({u: (this.__wxU || '').slice(0, 120), t: kind + '_' + (this.status || ''), at: Date.now()});
            if (window.__wxDiag.upErrors.length > 10) window.__wxDiag.upErrors.shift();
          }
        };
        this.addEventListener('load',  () => onEnd('load'));
        this.addEventListener('error', () => onEnd('error'));
        this.addEventListener('abort', () => onEnd('abort'));
        this.addEventListener('timeout', () => onEnd('timeout'));
      }
    } catch(e){}
    return _send.apply(this, arguments);
  };
  const _f = window.fetch;
  window.fetch = function(input, init){
    let u = '';
    try { u = typeof input === 'string' ? input : (input && input.url) || ''; } catch(e){}
    let sz = 0;
    try {
      if (isUp(u) && init && init.body) {
        const b = init.body;
        sz = b.size || b.byteLength || (typeof b === 'string' ? b.length : 0) || 0;
      }
    } catch(e){}
    if (isUp(u) && sz > 0) {
      window.__wxDiag.upRequests++;
      window.__wxDiag.upBytes += sz;
      window.__wxDiag.upLastSentAt = Date.now();
      window.__wxDiag.upInflight++;
    }
    return _f.apply(this, arguments).then(r => {
      try {
        if (isUp(u) && sz > 0) {
          window.__wxDiag.upInflight = Math.max(0, window.__wxDiag.upInflight - 1);
          if (r.ok) window.__wxDiag.upDoneOk++;
          else { window.__wxDiag.upDoneFail++; window.__wxDiag.upErrors.push({u: u.slice(0,120), t: 'fetch_' + r.status, at: Date.now()}); }
        }
      } catch(e){}
      return r;
    }).catch(err => {
      try {
        if (isUp(u) && sz > 0) {
          window.__wxDiag.upInflight = Math.max(0, window.__wxDiag.upInflight - 1);
          window.__wxDiag.upDoneFail++;
          window.__wxDiag.upErrors.push({u: u.slice(0,120), t: 'fetch_throw', m: String(err).slice(0,80), at: Date.now()});
        }
      } catch(e){}
      throw err;
    });
  };
  const _slice = Blob.prototype.slice;
  Blob.prototype.slice = function(){
    try { return _slice.apply(this, arguments); }
    catch(e) {
      window.__wxDiag.readErrors.push({t: 'slice_throw', m: String(e).slice(0,80), at: Date.now()});
      if (window.__wxDiag.readErrors.length > 10) window.__wxDiag.readErrors.shift();
      throw e;
    }
  };
  const wrapReader = (name) => {
    const orig = FileReader.prototype[name];
    if (!orig) return;
    FileReader.prototype[name] = function(){
      const fr = this;
      const onErr = () => {
        try {
          window.__wxDiag.readErrors.push({t: name + '_error', e: String(fr.error || '').slice(0,80), at: Date.now()});
          if (window.__wxDiag.readErrors.length > 10) window.__wxDiag.readErrors.shift();
        } catch(e){}
      };
      fr.addEventListener('error', onErr);
      fr.addEventListener('abort', onErr);
      return orig.apply(this, arguments);
    };
  };
  wrapReader('readAsArrayBuffer'); wrapReader('readAsBinaryString'); wrapReader('readAsDataURL'); wrapReader('readAsText');
}
"""


async def _install_upload_diagnostics(page):
    """注入：1) XHR/fetch 上传字节统计  2) FileReader/Blob.slice 错误捕获"""
    for fr in page.frames:
        try:
            await fr.evaluate(_UPLOAD_DIAG_JS)
        except Exception:
            pass


async def _read_upload_diagnostics(page) -> dict:
    agg = {
        'upRequests': 0, 'upBytes': 0, 'upLastSentAt': 0,
        'upDoneOk': 0, 'upDoneFail': 0, 'upInflight': 0,
        'upErrors': [], 'readErrors': []
    }
    for fr in page.frames:
        try:
            d = await fr.evaluate('() => window.__wxDiag || null')
        except Exception:
            d = None
        if not d:
            continue
        agg['upRequests'] += d.get('upRequests') or 0
        agg['upBytes'] += d.get('upBytes') or 0
        agg['upDoneOk'] += d.get('upDoneOk') or 0
        agg['upDoneFail'] += d.get('upDoneFail') or 0
        agg['upInflight'] += d.get('upInflight') or 0
        agg['upLastSentAt'] = max(agg['upLastSentAt'], d.get('upLastSentAt') or 0)
        for e in (d.get('upErrors') or [])[-5:]:
            agg['upErrors'].append(e)
        for e in (d.get('readErrors') or [])[-5:]:
            agg['readErrors'].append(e)
    return agg


async def _wx_publish_button_ready(page) -> tuple[bool, str]:
    """检查页面上的「发表」按钮是否处于可点击状态。

    视频号发表页在视频上传/转码进行中会把 `<button>发表</button>` 置为
    `disabled` 或加上 `weui-desktop-btn_disabled` / loading 类；后端处理完成、
    可以发布之后才解除禁用。这是比 DOM 文案更稳的「真正完成」信号。

    返回 (ready, hint)。当任意 frame 找到一个文本严格为「发表」「发表视频」
    「立即发表」、可见且未被 disabled 的按钮，即视为就绪。
    """
    js = r"""() => {
        function visible(el) {
            if (!el || !el.isConnected) return false;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') return false;
            if (parseFloat(s.opacity || '1') < 0.05) return false;
            const r = el.getBoundingClientRect();
            return r.width >= 24 && r.height >= 14;
        }
        const btns = document.querySelectorAll(
            'button, [role="button"], .weui-desktop-btn, .weui-desktop-btn_primary'
        );
        let sawDisabled = null;
        for (const b of btns) {
            const txt = ((b.innerText || b.textContent || '') + '').trim();
            if (!/^(发\s*表|发表视频|立即发表)$/.test(txt)) continue;
            if (!visible(b)) continue;
            const cls = ((b.className || '') + ' ' + (b.getAttribute('class') || '')).toLowerCase();
            const aria = (b.getAttribute('aria-disabled') || '').toLowerCase();
            const disabledFlag = b.disabled
                || /(?:^|\s)(?:disabled|disable|loading|busy|btn[-_]?disabled|weui-desktop-btn_disabled|forbid)(?:$|\s)/.test(cls)
                || aria === 'true';
            if (disabledFlag) {
                sawDisabled = { reason: 'disabled', txt, cls: cls.slice(0, 60) };
                continue;
            }
            return { ok: true, reason: 'ready', txt };
        }
        return { ok: false, reason: sawDisabled ? sawDisabled.reason : 'not-found',
                 hint: sawDisabled ? sawDisabled.cls : '' };
    }"""
    for fr in page.frames:
        try:
            r = await fr.evaluate(js)
        except Exception:
            r = None
        if r and r.get('ok'):
            return True, f"{r.get('txt', '发表')}({r.get('reason', '')})"
    return False, ''


def _format_diag(diag: dict) -> str:
    mib = (diag['upBytes'] or 0) / (1024 * 1024)
    last = diag.get('upLastSentAt') or 0
    if last:
        age = int(time.time() - last / 1000.0)
        last_str = f'{age}s 前' if age >= 0 else 'ahead'
    else:
        last_str = '从未'
    parts = [
        f"req={diag['upRequests']}({diag['upInflight']}飞行)",
        f"上行={mib:.2f}MiB",
        f"OK/失败={diag['upDoneOk']}/{diag['upDoneFail']}",
        f"上次发包={last_str}",
    ]
    if diag.get('upErrors'):
        e = diag['upErrors'][-1]
        parts.append(f"最近网络错={e.get('t')} {e.get('u', '')[:40]}")
    if diag.get('readErrors'):
        e = diag['readErrors'][-1]
        parts.append(f"最近读文件错={e.get('t')} {e.get('e', e.get('m', ''))[:40]}")
    return ' | '.join(parts)


def _upload_wait_max_seconds(video_path: str | None) -> int:
    """
    上传阶段最长等待（秒）。大文件 + 慢上行需要更久，可用环境变量覆盖。
    UPLOAD_WAIT_MAX_SEC：默认 7200（2 小时）；若本地文件超过 400MiB 会与按体积估算的下限取较大值。
    """
    try:
        base = int(os.environ.get('UPLOAD_WAIT_MAX_SEC', '7200') or '7200')
    except ValueError:
        base = 7200
    if base < 600:
        base = 600
    floor_by_size = 0
    if video_path and os.path.isfile(video_path):
        try:
            mib = os.path.getsize(video_path) / (1024 * 1024)
            # 粗估：每 100MiB 至少给 12 分钟上传+转码缓冲（仅作下限，与 base 取 max）
            floor_by_size = int((mib / 100.0) * 720)
        except OSError:
            pass
    return min(max(base, floor_by_size), 28800)


def record_schedule_interval_minutes(record: dict) -> int:
    """表单「间隔(分钟)」写入队列后供服务端使用：判定「距现在 > 间隔+5 分钟」才走微信定时；默认 1。

    也可用环境变量 SCHEDULE_INTERVAL_MIN（无表单/无字段时兜底）。
    """
    try:
        v = int(record.get('schedule_interval_min', record.get('interval_min', 0)) or 0)
    except (TypeError, ValueError):
        v = 0
    if 0 < v <= 1440:
        return v
    try:
        return max(1, int(os.environ.get('SCHEDULE_INTERVAL_MIN', '1') or '1'))
    except ValueError:
        return 1


def publish_timer_allowed(record: dict, target: datetime, now: datetime | None = None) -> tuple[bool, str]:
    """是否应在微信里勾选「定时」并按 target 的几点几分填写（不改动用户指定的分）。

    - 目标时间不晚于当前：不定时，直接发表。
    - 与现在相差 ≤「间隔分钟 + 5 分钟」：视为过近，不定时，直接发表。
    - 与现在相差 >「间隔分钟 + 5 分钟」：直接定时（保留用户填写的时分）。
    """
    now = now or datetime.now()
    delta = target - now
    if delta.total_seconds() <= 0:
        return False, 'past'
    iv = record_schedule_interval_minutes(record)
    if delta <= timedelta(minutes=iv + 5):
        return False, 'too_close'
    return True, 'ok'


async def wait_for_upload_with_progress(page, abort_signal, video_path=None, record=None):
    """Wait for video upload to complete by detecting page changes.
    先检测上传是否已启动（页面出现进度/文件名等变化），再等上传完成。
    不依赖 networkidle，因为页面背景流量会干扰检测。
    若页面百分比长时间不变（默认 900s），返回 stuck_progress，避免空等到总超时。

    重要修复：
    - 完成判定**优先**明确文案（上传成功等）；并支持**封面图/预览 video 已生成**
      （视频号分片传完后常无「上传成功」字，但会出 CDN 封面）。
    - 不再用 `[class*="success"]` 等宽泛 class（曾导致 `Upload complete (0s)` 误报）。
    - 文案命中：elapsed >= min_upload_sec 且 pct >= 95（如有 pct）。
    - 封面命中：elapsed >= min_upload_sec 且（pct>=88 或 上行字节 >= 文件约 86%），
      且 `_wx_upload_cover_ready` 在媒体区内检测到微信 CDN 图或可见预览 video。
    - 兜底：上行已达体积 ~86%、（进度>=92% **或** 进度条已消失 pct=None）、
      且 >=UPLOAD_TAIL_IDLE_SEC 无新上行 → 视为尾部处理完成。
    - 进度/完成均扫描所有 frames（micro/content iframe）。

    上传阶段只负责等待视频真正完成，不在过程中并行执行封面、位置、短剧、定时等操作，
    避免与上传握手/转码阶段互相干扰。
    """
    _ = record
    start_time = time.time()
    max_wait = _upload_wait_max_seconds(video_path)
    # 文件越大，最小上传时长越长 —— 给完成判定一个最低门槛，防止 0s 内"已完成"
    min_upload_sec = 15
    if video_path and os.path.isfile(video_path):
        try:
            mib = os.path.getsize(video_path) / (1024 * 1024)
            # 极保守：按 100 Mbps（≈12 MiB/s）估算的"理论最快"，再除以 2 当下限
            min_upload_sec = max(15, int(mib / 24))
        except OSError:
            pass
    logger.info(
        f'  等待上传完成：最长 {max_wait}s（UPLOAD_WAIT_MAX_SEC），'
        f'最早完成 {min_upload_sec}s 后才判（按文件大小估算），'
        f'同进度卡住阈值见 UPLOAD_STUCK_SAME_PCT_SEC'
    )

    # 进度条选择器：保留较宽，只用来识别 upload_started，不参与"完成"判定
    progress_selectors = [
        'progress', '.weui-desktop-progress',
        '[class*="progress-bar"]', '[class*="ProgressBar"]',
        '[class*="percent"]:not([class*="cpu"]):not([class*="battery"])',
        'text=/上传中|uploading|\\d+%/i',
    ]
    # 完成判定：只信明确文案。绝对不要 `[class*="success"]` 之类的宽泛 class 选择器。
    done_selectors = [
        'text=/上传成功|发表成功|发布成功|视频上传完成|视频处理完成|转码完成|上传完成/i',
        'text=/upload\\s*(?:successful|complete|completed|finished)/i',
    ]

    upload_started = False
    last_pct = None
    last_pct_change = start_time
    try:
        # 大文件某一百分比可能持续较久（多段上传/转码），默认 15 分钟同 % 才判卡住
        stuck_same_sec = int(os.environ.get('UPLOAD_STUCK_SAME_PCT_SEC', '900') or '900')
    except ValueError:
        stuck_same_sec = 900
    if stuck_same_sec < 0:
        stuck_same_sec = 0
    logger.info(f'  同进度卡住判定：{stuck_same_sec}s 无变化（0=关闭）')

    # 网络无活动判定（默认 90s 无任何上传字节出新增），用于尽早识别"前端假装上传"
    try:
        no_net_sec = int(os.environ.get('UPLOAD_NO_NET_SEC', '90') or '90')
    except ValueError:
        no_net_sec = 90
    last_bytes = 0
    last_bytes_change = start_time
    last_diag_log = 0.0

    # 注入诊断 hook（XHR/fetch 字节、Blob/FileReader 错误）
    await _install_upload_diagnostics(page)

    async def _any_frame_has(sel: str) -> bool:
        for fr in page.frames:
            try:
                if await fr.locator(sel).count() > 0:
                    return True
            except Exception:
                pass
        return False

    while time.time() - start_time < max_wait:
        if _check_abort(abort_signal):
            logger.warn('  Upload aborted by user')
            return 'aborted'

        # 重新注入一次（页面 frame 变化时），开销极小（已有 __wxDiag 时立刻返回）
        await _install_upload_diagnostics(page)

        # 检测上传是否在进行中
        if not upload_started:
            for sel in progress_selectors:
                if await _any_frame_has(sel):
                    upload_started = True
                    break

        pct = await _extract_upload_percent(page)
        now = time.time()
        if upload_started and pct is not None and stuck_same_sec > 0:
            page._wx_last_pct = pct # 保存进度供后台填写判断
            if last_pct is None or pct != last_pct:
                if last_pct is not None and pct != last_pct:
                    logger.info(f'  上传进度 {last_pct}% → {pct}%')
                elif last_pct is None:
                    logger.info(f'  检测到上传进度约 {pct}%（若长期不变可能为网络/服务端问题）')
                last_pct = pct
                last_pct_change = now
            elif last_pct is not None and pct == last_pct:
                stall = now - last_pct_change
                if stall >= stuck_same_sec:
                    logger.warn(
                        f'  上传进度停在 {pct}% 已超过 {int(stall)}s '
                        f'（阈值 UPLOAD_STUCK_SAME_PCT_SEC={stuck_same_sec}），判定为卡住'
                    )
                    return 'stuck_progress'

        # 读网络/读文件诊断
        diag = await _read_upload_diagnostics(page)
        if diag['upBytes'] > last_bytes:
            last_bytes = diag['upBytes']
            last_bytes_change = now
        bytes_idle = now - last_bytes_change

        # 检测上传是否已完成
        elapsed = time.time() - start_time
        file_bytes = int(getattr(page, '_wx_upload_source_bytes', 0) or 0)
        if file_bytes <= 0 and video_path and os.path.isfile(video_path):
            try:
                file_bytes = os.path.getsize(video_path)
            except OSError:
                file_bytes = 0
        up_b = int(diag.get('upBytes') or 0)
        bytes_nearly_done = file_bytes > 0 and up_b >= int(file_bytes * 0.86)

        if upload_started and elapsed >= min_upload_sec:
            pct_ok_done = (pct is None) or (pct >= 95)
            if pct_ok_done:
                for sel in done_selectors:
                    if await _any_frame_has(sel):
                        logger.info(
                            f'  Upload complete ({int(elapsed)}s, '
                            f'pct={pct}, matched="{sel}")'
                        )
                        return 'ok'

            # 封面 / 预览已出现（视频号常见「静默完成」）
            pct_ok_cover = (pct is None) or (pct >= 88)
            cover_ok, cover_hint = await _wx_upload_cover_ready(page)
            if cover_ok and (pct_ok_cover or bytes_nearly_done):
                logger.info(
                    f'  Upload complete ({int(elapsed)}s, pct≈{pct}, '
                    f'上行≈{up_b / 1048576:.1f}MiB, 封面/预览就绪 {cover_hint[:140]})'
                )
                return 'ok'

            # 强信号：体积已饱和（>=86%）后，「发表」按钮变为可点。
            # 视频号前端在转码/处理期间会把发表按钮置 disabled，一旦解除即代表后端已就绪，
            # 比 22s 无新包兜底要更早、也更确定。
            if bytes_nearly_done and (pct is None or pct >= 88):
                pub_ok, pub_hint = await _wx_publish_button_ready(page)
                if pub_ok:
                    logger.info(
                        f'  Upload complete ({int(elapsed)}s, pct={pct}, '
                        f'上行≈{up_b / 1048576:.1f}MiB, 发表按钮已可点 {pub_hint})'
                    )
                    return 'ok'

            # 无封面 DOM 但体积已饱和、且无新包一段时间（尾部转码/合并）。
            # 注意：进度到 99% 后 ant-progress 常被移除，_extract_upload_percent 会得到 None，
            # 若仍要求 pct>=96 则永远不会结束 —— 必须允许 pct is None。
            try:
                tail_idle = float(os.environ.get('UPLOAD_TAIL_IDLE_SEC', '22') or '22')
            except ValueError:
                tail_idle = 22.0
            pct_high_or_gone = (pct is None) or (pct >= 92)
            if bytes_nearly_done and pct_high_or_gone and bytes_idle >= tail_idle:
                logger.info(
                    f'  Upload complete ({int(elapsed)}s, pct={pct}, '
                    f'上行≈{up_b / 1048576:.1f}MiB, 无新包≥{int(tail_idle)}s 判定尾部完成)'
                )
                return 'ok'

        # 网络层诊断：若上传开始后超过 no_net_sec 没有新增上行字节
        if (
            upload_started and no_net_sec > 0
            and diag['upBytes'] > 0
            and bytes_idle >= no_net_sec
        ):
            cover_ok2, cover_h2 = await _wx_upload_cover_ready(page)
            if cover_ok2 and ((pct is None or pct >= 85) or bytes_nearly_done):
                logger.info(
                    f'  Upload complete ({int(elapsed)}s, 无新字节但封面就绪 {cover_h2[:120]})'
                )
                return 'ok'
            # 上行已达文件绝大部分后长期无新包：进度条可能已消失（pct=None），仍判完成
            if bytes_nearly_done and (pct is None or pct >= 90):
                logger.info(
                    f'  Upload complete ({int(elapsed)}s, 无新字节≥{no_net_sec}s, '
                    f'pct={pct}, 上行≈{up_b / 1048576:.1f}MiB, 判定分片已结束)'
                )
                return 'ok'
            logger.warn(
                f'  ⚠ 上传字节流停止：累计 {diag["upBytes"]/1048576:.2f}MiB 已 '
                f'{int(bytes_idle)}s 无新增（阈值 UPLOAD_NO_NET_SEC={no_net_sec}）；'
                f'诊断：{_format_diag(diag)}'
            )
            return 'stuck_progress'

        # 每 10 秒报一次详细状态（含网络字节）
        if now - last_diag_log >= 10:
            last_diag_log = now
            tag = 'Uploading' if upload_started else 'Waiting'
            extra = f' 页面约 {pct}%' if (upload_started and pct is not None) else ''
            logger.info(f'  {tag} {int(elapsed)}s{extra} | {_format_diag(diag)}')

        await page.wait_for_timeout(2000)

    if upload_started:
        logger.warn(f'  Upload timeout ({max_wait}s)')
        return 'timeout'
    logger.warn('  Upload never started')
    return 'not_started'


def _path_has_non_ascii(s: str) -> bool:
    return not all(ord(c) < 128 for c in s)


def _ensure_ascii_upload_path(src_path: str):
    """
    在以下情况复制到系统临时目录，**仅改文件名、不改扩展名**（勿把 .mov 伪装成 .mp4 内容）：
    - 路径任意位置含非 ASCII；或
    - **文件名（basename）含中文等非 ASCII**：改为时间戳式纯 ASCII 名，减少 Chromium/微信页对 Unicode 路径的兼容问题。
    """
    src_path = os.path.abspath(os.path.normpath(src_path))
    raw_ext = os.path.splitext(src_path)[1].lower() or '.mp4'
    base = os.path.basename(src_path)
    need_temp = _path_has_non_ascii(src_path) or _path_has_non_ascii(base)

    if not need_temp:

        def _noop():
            return None

        return src_path, _noop

    tmp = os.path.join(
        tempfile.gettempdir(),
        f'wx_upl_{time.time_ns()}_{os.getpid()}{raw_ext}',
    )
    try:
        if os.path.lexists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    shutil.copy2(src_path, tmp)
    if _path_has_non_ascii(base):
        logger.info(
            '  文件名含非 ASCII：已复制为时间戳式纯英文文件名（同扩展名）供浏览器挂接'
        )
    else:
        logger.info('  路径含非 ASCII：已复制到临时目录（保持原扩展名）')

    def _cleanup():
        try:
            if os.path.lexists(tmp):
                os.remove(tmp)
        except OSError:
            pass

    return tmp, _cleanup


def _set_files_timeout_ms(local_path: str) -> float:
    """大文件挂接需要更长超时（默认 30s 不够）。"""
    try:
        sz = os.path.getsize(local_path)
    except OSError:
        return 180000.0
    # 每 200MB +60s，下限 120s，上限 15min
    extra = (sz // (200 * 1024 * 1024)) * 60000
    return float(min(900000, max(120000, 120000 + extra)))


def _maybe_remux_to_mp4(src_path: str):
    """
    若源是 .mov/.m4v 等且本机有 ffmpeg：
      - 用 ffprobe 检测 video codec
      - h264 + (aac|mp3) → 直接 -c copy remux 到 .mp4（秒级，不重新编码）
      - hevc/prores 等浏览器不能直接播的 → 不 remux（需重编码，太慢；交给用户）
    返回 (new_path, cleanup_fn) 或 (None, None)。
    禁用：SKIP_FFMPEG_REMUX=1
    """
    if os.environ.get('SKIP_FFMPEG_REMUX', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return None, None
    ext = os.path.splitext(src_path)[1].lower()
    if ext == '.mp4':
        return None, None
    if not shutil_which('ffmpeg') or not shutil_which('ffprobe'):
        if ext in ('.mov', '.m4v'):
            logger.info(
                '  本机未装 ffmpeg/ffprobe：跳过自动 remux。建议执行 `brew install ffmpeg` 后再试，'
                '本工具会自动把 H.264 的 .mov 秒级重新封装为 .mp4。'
            )
        return None, None
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', src_path],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(result.stdout or '{}')
    except Exception as e:
        logger.warn(f'  ffprobe 检测失败，跳过 remux: {e}')
        return None, None
    streams = data.get('streams') or []
    vcodec = next((s.get('codec_name', '') for s in streams if s.get('codec_type') == 'video'), '')
    acodec = next((s.get('codec_name', '') for s in streams if s.get('codec_type') == 'audio'), '')
    vcodec = (vcodec or '').lower()
    acodec = (acodec or '').lower()
    if vcodec != 'h264':
        logger.warn(
            f'  视频编码为 {vcodec or "未知"}（非 H.264），浏览器很可能拒绝。'
            '请在外部用 FFmpeg 重新编码：'
            'ffmpeg -i in.mov -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart out.mp4'
        )
        return None, None
    if acodec not in ('aac', 'mp3', ''):
        logger.info(f'  音频编码 {acodec} 非 aac/mp3，将 -c:a aac 转码（视频仍 -c:v copy）')
        a_args = ['-c:a', 'aac']
    else:
        a_args = ['-c:a', 'copy']
    # 优先放在源文件同目录（uploads/），避免 macOS /var/folders 带来的 Chromium 文件访问差异；
    # 写入失败再回落到系统临时目录。
    src_dir = os.path.dirname(os.path.abspath(src_path))
    base_name = f'wx_remux_{time.time_ns()}_{os.getpid()}.mp4'
    out = os.path.join(src_dir, base_name)
    if not os.access(src_dir, os.W_OK):
        out = os.path.join(tempfile.gettempdir(), base_name)
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', src_path,
           '-c:v', 'copy', *a_args, '-movflags', '+faststart', out]
    logger.info('  发现可 remux：H.264 视频流，正在 ffmpeg -c copy 重新封装为 .mp4（不重新编码）…')
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        logger.warn(f'  ffmpeg remux 异常: {e}')
        return None, None
    if r.returncode != 0 or not (os.path.exists(out) and os.path.getsize(out) > 0):
        logger.warn(f'  ffmpeg remux 失败 (rc={r.returncode}): {(r.stderr or "")[:300]}')
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        return None, None
    logger.info(
        f'  remux 完成：{os.path.getsize(out) / 1024 / 1024:.1f} MiB，'
        f'耗时 {time.time() - t0:.1f}s → 改用此 .mp4 上传'
    )

    def _cleanup():
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass

    return out, _cleanup


async def _inspect_input_files(inp):
    """读取 input.files 详细信息（length + 每项 name/size/type），用于诊断前端是否清空。"""
    try:
        return await inp.evaluate(
            """el => {
                const fs = el.files;
                if (!fs) return { length: 0, items: [] };
                return {
                    length: fs.length,
                    items: Array.from(fs).slice(0, 3).map(f => ({
                        name: f.name, size: f.size, type: f.type
                    }))
                };
            }"""
        )
    except Exception:
        return None


async def _save_upload_debug(page, tag: str):
    """失败时保存截图与 HTML，便于线下比对真机操作 DOM。"""
    try:
        if not os.path.exists(SCREENSHOTS_DIR):
            os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = int(time.time() * 1000)
        png = os.path.join(SCREENSHOTS_DIR, f'upload_fail_{ts}_{tag}.png')
        html = os.path.join(SCREENSHOTS_DIR, f'upload_fail_{ts}_{tag}.html')
        await page.screenshot(path=png, full_page=True)
        with open(html, 'w', encoding='utf-8') as f:
            f.write(await page.content())
        logger.info(f'  已保存调试快照：{os.path.basename(png)} / .html')
    except Exception as e:
        logger.info(f'  保存调试快照失败: {e}')


async def _prime_upload_zone(page):
    """部分页面需先点上传区，file input 才会接受 set_input_files。"""
    try:
        # 尝试多个可能的点击目标，增加鲁棒性
        hints = [
            '拖拽', '点击上传', '上传视频', '选择视频',
            'div.post-add-media', '.upload-area', '.media-status-body'
        ]
        for h in hints:
            try:
                if h.startswith('.') or ' ' in h or '>' in h:
                    loc = page.locator(h).first
                else:
                    loc = page.get_by_text(re.compile(h)).first
                
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    # 只要点中一个有效的就退出
                    return
            except Exception:
                continue
    except Exception:
        pass


def _frame_url_bonus(fr_url: str) -> int:
    """微信发表页里真正承载视频上传的是嵌套的 micro/content/post/create iframe，
    主 frame 也有同结构 input，但「挂在主 frame」常被忽略。所以含 micro/content 的优先。"""
    u = (fr_url or '').lower()
    if 'micro/content' in u:
        return 1000
    return 0


async def _collect_video_file_inputs(page):
    """遍历主文档与所有 iframe 内的 input[type=file]（主 frame 的 locator 不含子 frame）。"""
    out = []
    for fr in page.frames:
        try:
            try:
                fr_url = fr.url or ''
            except Exception:
                fr_url = ''
            loc = fr.locator('input[type=file]')
            n = await loc.count()
            for i in range(n):
                acc = await loc.nth(i).get_attribute('accept') or ''
                pr = _accept_priority_for_video(acc) + _frame_url_bonus(fr_url)
                out.append((fr, i, pr, acc, fr_url))
        except Exception:
            continue
    out.sort(key=lambda x: -x[2])
    return out


async def _wait_for_micro_iframe(page, timeout_ms: int = 12000) -> bool:
    """等待 micro/content/post/create iframe **里的 file input 真正出现**
    （视频号发表页的真正承载者；只看 URL 出现还太早，里面 input 没渲染好）。
    返回 True 表示已出现；False 表示超时（仍继续）。"""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            for fr in page.frames:
                u = (getattr(fr, 'url', '') or '').lower()
                if 'micro/content' not in u:
                    continue
                try:
                    n = await fr.locator('input[type=file]').count()
                except Exception:
                    n = 0
                if n > 0:
                    logger.info(
                        f'  已等到 micro iframe 内 file input：'
                        f'frames={len(page.frames)}, micro_inputs={n}'
                    )
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(300)
    logger.info(
        f'  micro iframe 内 input 等待超时（{timeout_ms / 1000:.0f}s），基于现有 frame 继续'
    )
    return False


async def select_short_drama(page, drama_name):
    """
    发表页里绑定「视频号剧集」短剧链接（可选步骤）。

    - CSV / 前端字段名：short_drama_name；页面上对应输入框「如：协议生四胎」(#formDrama)。
    - 自动化步骤：点「选择链接」→ 选「视频号剧集」→ 在「搜索内容」里填入剧集名并回车
      → 尝试点击表格首行选中剧集。
    """
    logger.info(f'  选择剧集：{drama_name}')
    try:
        base = (
            page.locator('.link-selector').get_by_text('选择链接')
            .or_(page.get_by_text('选择链接', exact=True))
        )
        await base.first.click()
        await page.get_by_text('视频号剧集', exact=True).wait_for(state='visible', timeout=5000)
        await page.get_by_text('视频号剧集', exact=True).click()
        await page.get_by_text('选择需要添加的视频号剧集').wait_for(state='visible', timeout=5000)
        await page.get_by_text('选择需要添加的视频号剧集').click()
        sb = page.get_by_role('textbox', name='搜索内容')
        await sb.wait_for(state='visible', timeout=5000)
        await sb.fill(drama_name)
        await page.keyboard.press('Enter')
        await page.wait_for_timeout(1000)
        for loc in [
            page.locator('table tbody tr').first,
            page.get_by_role('row').nth(1),
        ]:
            try:
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    return
            except Exception:
                pass
        logger.warn(f'  Drama "{drama_name}" not found')
        await page.keyboard.press('Escape')
    except Exception as e:
        logger.warn(f'  Drama failed: {e}')


async def set_cover(page, cover_path):
    """Upload a custom cover image for the video."""
    logger.info('  Setting cover')
    if not os.path.exists(cover_path):
        logger.warn(f'  Cover not found: {cover_path}')
        return
    try:
        cover_heading = page.get_by_role('heading', name='编辑封面')
        if not await cover_heading.is_visible():
            edit_candidates = [
                page.locator('.edit-btn:visible').filter(has_text='编辑'),
                page.get_by_text('编辑', exact=True),
            ]
            opened = False
            for cand in edit_candidates:
                try:
                    count = await cand.count()
                except Exception:
                    count = 0
                for idx in range(count):
                    try:
                        await cand.nth(idx).click(force=True, timeout=3000)
                        await cover_heading.wait_for(state='visible', timeout=1500)
                        opened = True
                        break
                    except Exception:
                        try:
                            await page.keyboard.press('Escape')
                        except Exception:
                            pass
                if opened:
                    break
            if not opened:
                raise RuntimeError('未找到可打开“编辑封面”的编辑按钮')
        await page.get_by_text('上传封面', exact=True).click()
        await page.locator('input[type=file]').nth(1).set_input_files(cover_path)
        await page.wait_for_timeout(2000)
        await page.get_by_role('button', name='确认').click()
        logger.info('  Cover set')
    except Exception as e:
        logger.warn(f'  Cover failed: {e}')


# 视频号「发表时间」WeUI（参考线上 DOM）：
#   div.form-item > .label「发表时间」> .form-item-body >
#   dl.weui-desktop-picker__date.weui-desktop-picker__date-time
#     > dt > input.readonly placeholder「请选择发表时间」+ i.weui-desktop-icon__date
#     > dd.weui-desktop-picker__dd（默认 style display:none，展开后显示月历）
#     > … panel-fd 内嵌 dl.weui-desktop-picker__time + 时分 ol（li 选中为 .weui-desktop-picker__selected）
# 逻辑：Shadow 穿透 + document 兜底 + .form-item 查找 + panel_day 向上 closest(dl)。
_JS_PICKER_FIND_DLS = r"""
function __cls(n) { return (n && n.className && n.className.toString()) || ''; }
function __collectDls(root, token) {
  var out = [];
  var seen = new Set();
  function walk(node) {
    if (!node) return;
    if (node.nodeType === 1) {
      if (seen.has(node)) return;
      seen.add(node);
      var cn = __cls(node);
      if (node.tagName === 'DL' && cn.indexOf(token) >= 0) out.push(node);
      if (node.shadowRoot) walk(node.shadowRoot);
      for (var c = node.firstElementChild; c; c = c.nextElementSibling) walk(c);
    } else if (node.nodeType === 11) {
      for (var c2 = node.firstElementChild; c2; c2 = c2.nextElementSibling) walk(c2);
    }
  }
  walk(root);
  return out;
}
function __pubDateInputs(dl) {
  var inps = dl.querySelectorAll('input');
  for (var i = 0; i < inps.length; i++) {
    var ph = (inps[i].placeholder || '');
    if (ph.indexOf('发表时间') >= 0 || ph.indexOf('发表') >= 0) return inps[i];
  }
  return dl.querySelector('input[placeholder*="发表时间"], input[placeholder*="发表"]');
}
function __pubTimeInputs(dl) {
  var hit = dl.querySelector('input[placeholder*="请选择时间"]');
  if (hit) return hit;
  var inps = dl.querySelectorAll('input');
  for (var j = 0; j < inps.length; j++) {
    var ph = (inps[j].placeholder || '');
    if (ph.indexOf('请选择时间') >= 0) return inps[j];
  }
  return null;
}
function __findDateDlByPanelWalk(wrap) {
  // class 或 Teleport 导致匹配不到 date-time token 时：从「月历面板」向上找外层 dl（与你提供的 DOM 一致）
  var doc = wrap.ownerDocument || document;
  var de = doc.documentElement || doc.body;
  if (!de) return null;
  var stack = [de];
  var seen = new Set();
  while (stack.length) {
    var n = stack.pop();
    if (!n || seen.has(n)) continue;
    seen.add(n);
    if (n.nodeType === 1) {
      var cn = __cls(n);
      if (cn.indexOf('weui-desktop-picker__panel_day') >= 0 || cn.indexOf('picker__panel_day') >= 0) {
        var r = n.getBoundingClientRect();
        if (r.width > 20 && r.height > 20) {
          var dl = n.closest('dl');
          if (dl) return dl;
        }
      }
      if (n.shadowRoot) stack.push(n.shadowRoot);
      for (var c = n.firstElementChild; c; c = c.nextElementSibling) stack.push(c);
    } else if (n.nodeType === 11) {
      for (var c2 = n.firstElementChild; c2; c2 = c2.nextElementSibling) stack.push(c2);
    }
  }
  return null;
}
function __findDateDlFromFormItemDoc(wrap) {
  var doc = wrap.ownerDocument || document;
  var fis = doc.querySelectorAll('div.form-item');
  for (var i = 0; i < fis.length; i++) {
    var lab = fis[i].querySelector('.label');
    var t = (lab && (lab.textContent || '').replace(/\s+/g, '')) || '';
    if (t.indexOf('发表时间') < 0) continue;
    var dl = fis[i].querySelector('dl[class*="weui-desktop-picker__date"]');
    if (!dl) continue;
    var cn = __cls(dl);
    if (cn.indexOf('weui-desktop-picker__time') >= 0 && cn.indexOf('weui-desktop-picker__date-time') < 0) continue;
    return dl;
  }
  return null;
}
function __findDateDl(wrap) {
  var token = 'weui-desktop-picker__date-time';
  var list = __collectDls(wrap, token);
  if (!list.length) {
    var doc = wrap.ownerDocument || document;
    var de = doc.documentElement || doc.body;
    if (de) list = __collectDls(de, token);
  }
  if (!list.length) {
    var via = __findDateDlByPanelWalk(wrap);
    if (via) list = [via];
  }
  if (!list.length) {
    var fi = __findDateDlFromFormItemDoc(wrap);
    if (fi) list = [fi];
  }
  if (!list.length) return null;
  if (list.length === 1) return list[0];
  var wr = wrap.getBoundingClientRect();
  var best = null;
  var bestScore = 1e12;
  for (var i = 0; i < list.length; i++) {
    var cand = list[i];
    var inp = __pubDateInputs(cand);
    if (!inp) continue;
    var dr = cand.getBoundingClientRect();
    if (dr.width < 2 || dr.height < 2) continue;
    var dx = Math.abs(dr.left - wr.left);
    var dy = Math.max(0, wr.top - dr.top - 8);
    var panel = cand.querySelector('.weui-desktop-picker__panel_day, [class*="picker__panel_day"]');
    var parea = 0;
    if (panel) {
      var pr = panel.getBoundingClientRect();
      parea = pr.width * pr.height;
    }
    // 距离 wrap 仍为主序；同距离下优先「月历已铺开」的实例（避免命中隐藏/占位 dl）
    var score = dx + dy * 3 - parea * 1e-6;
    if (score < bestScore) { bestScore = score; best = cand; }
  }
  if (best) return best;
  return list[0];
}
function __findTimeDl(wrap) {
  // 视频号 DOM：`dl.weui-desktop-picker__time` 嵌在 `dl.weui-desktop-picker__date-time` 内的日期 dd
  //（panel-fd 里），与 date-time 平级的旧假设会找不到；且 Teleport 后只能从 dateDl 子树 query。
  var dateDl = __findDateDl(wrap);
  if (dateDl) {
    var nested = dateDl.querySelector('dl.weui-desktop-picker__time');
    if (!nested) {
      var dls = dateDl.querySelectorAll('dl');
      for (var i = 0; i < dls.length; i++) {
        var cn = __cls(dls[i]);
        if (cn.indexOf('weui-desktop-picker__time') >= 0 && cn.indexOf('weui-desktop-picker__date-time') < 0) {
          nested = dls[i];
          break;
        }
      }
    }
    if (nested) return nested;
  }
  var token = 'weui-desktop-picker__time';
  var list = __collectDls(wrap, token);
  if (!list.length && dateDl) list = __collectDls(dateDl, token);
  if (!list.length && dateDl && dateDl.parentElement) list = __collectDls(dateDl.parentElement, token);
  if (!list.length) {
    var doc = wrap.ownerDocument || document;
    var de = doc.documentElement || doc.body;
    if (de) list = __collectDls(de, token);
  }
  if (!list.length) return null;
  if (list.length === 1) return list[0];
  var wr = wrap.getBoundingClientRect();
  var best = null;
  var bestScore = 1e12;
  for (var j = 0; j < list.length; j++) {
    var cand = list[j];
    var inp = __pubTimeInputs(cand);
    if (!inp) continue;
    var dr = cand.getBoundingClientRect();
    if (dr.width < 2 || dr.height < 2) continue;
    var score = Math.abs(dr.left - wr.left) + Math.abs(dr.top - wr.bottom);
    if (score < bestScore) { bestScore = score; best = cand; }
  }
  if (best) return best;
  return list[0];
}
"""


async def _picker_radio_is_checked(w) -> bool:
    """「定时」是否选中：仅以原生 input.checked 为准（避免 label/icon 样式误判导致跳过点击「定时」）。"""
    try:
        return bool(
            await w.evaluate(
                r"""(root) => {
            const inp = root.querySelector('input.weui-desktop-form__radio[value="1"]');
            return !!(inp && inp.checked);
        }"""
            )
        )
    except Exception:
        return False


async def _picker_click_timer_radio(w, page) -> bool:
    """点击「定时」radio，保证触发 v-if 挂出「发表时间」表单项。"""
    radio_label = w.locator('label.weui-desktop-form__check-label').filter(
        has=w.locator('input.weui-desktop-form__radio[value="1"]')
    ).first
    if await radio_label.count() > 0:
        try:
            await radio_label.click(force=True, timeout=3500)
        except Exception:
            if not await _real_mouse_click(radio_label, page, ''):
                try:
                    await w.evaluate(r"""(root) => {
                        const inp = root.querySelector('input.weui-desktop-form__radio[value="1"]');
                        if (!inp) return;
                        const lab = inp.closest('label');
                        if (lab) lab.click(); else inp.click();
                        if (!inp.checked) {
                            inp.checked = true;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""")
                except Exception:
                    return False
    else:
        try:
            await w.evaluate(r"""(root) => {
                const inp = root.querySelector('input.weui-desktop-form__radio[value="1"]');
                if (!inp) return;
                const lab = inp.closest('label');
                if (lab) lab.click(); else inp.click();
                if (!inp.checked) {
                    inp.checked = true;
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""")
        except Exception:
            return False
    for _ in range(45):
        await page.wait_for_timeout(120)
        if await _picker_radio_is_checked(w):
            return True
    return False


async def _picker_dl_exists(fr, w) -> bool:
    """发表日期 dl 是否已挂载：JS + Playwright（含 div.form-item「发表时间」与 dl.weui-desktop-picker__date）。"""
    try:
        if bool(await w.evaluate(_JS_PICKER_FIND_DLS + '(root) => !!__findDateDl(root)')):
            return True
    except Exception:
        pass
    try:
        item = fr.locator('div.form-item').filter(has_text=re.compile(r'发表时间')).first
        if await item.count() > 0:
            dlf = item.locator('dl[class*="weui-desktop-picker__date"]').first
            if await dlf.count() > 0:
                return True
    except Exception:
        pass
    try:
        pan = fr.locator('.weui-desktop-picker__panel_day').first
        if await pan.count() > 0 and await pan.is_visible():
            return True
    except Exception:
        pass
    try:
        for sel in (
            'dl.weui-desktop-picker__date.weui-desktop-picker__date-time',
            'dl.weui-desktop-picker__date-time',
            'dl[class*="picker__date-time"]',
            'dl[class*="weui-desktop-picker__date"]',
        ):
            loc = fr.locator(sel).first
            if await loc.count() > 0:
                try:
                    if await loc.is_visible():
                        return True
                except Exception:
                    return True
    except Exception:
        pass
    try:
        if await fr.locator('input[readonly][placeholder*="发表时间"]').count() > 0:
            return True
    except Exception:
        pass
    return False


def _picker_pub_date_dl(fr):
    """「发表时间」WeUI 日期+时间一体 picker 的外层 dl（用 placeholder 限定，避免点到页上其它日期控件）。"""
    return fr.locator(
        'dl.weui-desktop-picker__date.weui-desktop-picker__date-time, '
        'dl.weui-desktop-picker__date-time'
    ).filter(has=fr.locator('input[placeholder*="发表时间"]')).first


def _picker_pub_time_dl(fr):
    """发表时间下的嵌套「请选择时间」时分 dl。"""
    # 关键：优先限定在当前「发表时间」date-time 组件内部，避免命中页面其它同名时间控件
    dt_focus = fr.locator(
        'dl.weui-desktop-picker__date.weui-desktop-picker__date-time.weui-desktop-picker__focus, '
        'dl.weui-desktop-picker__date.weui-desktop-picker__date-time'
    ).filter(has=fr.locator('input[placeholder*="发表时间"]')).first
    nested = dt_focus.locator('dl.weui-desktop-picker__time').filter(
        has=dt_focus.locator('input[placeholder*="请选择时间"]')
    ).first
    # Playwright Locator 惰性求值，直接返回 nested；若不存在，调用处会 count()==0 再走兜底
    return nested


async def _picker_date_dd_is_open(w, fr=None) -> bool:
    """日期月历是否已展开：以 panel_day / 日期格为准（勿单信 dd 的 display，Teleport 时易误判）。

    当 fr 传入时，先在整 frame 文档上扫一遍（与 Playwright「根 locator」是否同一子树无关），
    避免月历已开但 w 命中错误 .post-time-wrap 或 evaluate 根节点与真实面板不同步时误判未打开。
    """
    if fr is not None:
        try:
            if bool(
                await fr.evaluate(
                    _JS_PICKER_FIND_DLS
                    + r"""() => {
            var de = document.documentElement || document.body;
            if (!de) return false;
            var dls = __collectDls(de, 'weui-desktop-picker__date-time');
            for (var i = 0; i < dls.length; i++) {
              var dl = dls[i];
              if (!__pubDateInputs(dl)) continue;
              var panel = dl.querySelector('.weui-desktop-picker__panel_day, [class*="picker__panel_day"]');
              if (!panel) continue;
              var pr = panel.getBoundingClientRect();
              if (pr.width > 12 && pr.height > 12) return true;
              var anchors = dl.querySelectorAll(
                '.weui-desktop-picker__panel_day tbody td a, .weui-desktop-picker__panel_day tbody a'
              );
              if (anchors.length >= 8) return true;
              var trn = dl.querySelectorAll('.weui-desktop-picker__panel_day tbody tr').length;
              if (trn >= 4 && pr.width > 4 && pr.height > 4) return true;
            }
            return false;
        }"""
                )
            ):
                return True
        except Exception:
            pass
        try:
            pans = fr.locator('.weui-desktop-picker__panel_day')
            pc = await pans.count()
            for i in range(min(pc, 8)):
                box = await pans.nth(i).bounding_box()
                if box and box.get('width', 0) > 12 and box.get('height', 0) > 12:
                    return True
        except Exception:
            pass

    try:
        return bool(
            await w.evaluate(
                _JS_PICKER_FIND_DLS
                + r"""(root) => {
            const dl = __findDateDl(root);
            if (!dl) return false;
            const panel = dl.querySelector('.weui-desktop-picker__panel_day, [class*="picker__panel_day"]');
            if (panel) {
              const pr = panel.getBoundingClientRect();
              if (pr.width > 12 && pr.height > 12) return true;
            }
            const anchors = dl.querySelectorAll(
              '.weui-desktop-picker__panel_day tbody td a, .weui-desktop-picker__panel_day tbody a'
            );
            if (anchors.length >= 8) return true;
            let dd = null;
            for (let c = dl.firstElementChild; c; c = c.nextElementSibling) {
                if (c.tagName === 'DD' && c.classList.contains('weui-desktop-picker__dd') && !c.classList.contains('weui-desktop-picker__dd__time')) {
                    dd = c; break;
                }
            }
            if (!dd) return false;
            const st = getComputedStyle(dd);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            const r = dd.getBoundingClientRect();
            return r.width > 4 && r.height > 4;
        }"""
            )
        ) or False
    except Exception:
        return False


async def _picker_time_dd_is_open(w, fr=None) -> bool:
    """时分面板是否展开：以 ol 时分列表尺寸为准（与 date 同理，勿二次点 dt 关掉）。"""
    if fr is not None:
        try:
            if bool(
                await fr.evaluate(
                    _JS_PICKER_FIND_DLS
                    + r"""() => {
            var de = document.documentElement || document.body;
            if (!de) return false;
            var dls = __collectDls(de, 'weui-desktop-picker__time');
            for (var i = 0; i < dls.length; i++) {
              var timeDl = dls[i];
              if (__cls(timeDl).indexOf('weui-desktop-picker__date-time') >= 0) continue;
              if (!__pubTimeInputs(timeDl)) continue;
              var olh = timeDl.querySelector('ol.weui-desktop-picker__time__hour, ol[class*="time__hour"]');
              if (olh) {
                var r2 = olh.getBoundingClientRect();
                if (r2.width > 8 && r2.height > 20) return true;
              }
              var olm = timeDl.querySelector('ol.weui-desktop-picker__time__minute, ol[class*="time__minute"]');
              if (olm) {
                var r3 = olm.getBoundingClientRect();
                if (r3.width > 8 && r3.height > 20) return true;
              }
            }
            return false;
        }"""
                )
            ):
                return True
        except Exception:
            pass
        try:
            for sel in (
                'ol.weui-desktop-picker__time__hour',
                'ol[class*="weui-desktop-picker__time__hour"]',
            ):
                loc = fr.locator(sel).first
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box()
                if box and box.get('width', 0) > 8 and box.get('height', 0) > 20:
                    return True
        except Exception:
            pass

    try:
        return bool(
            await w.evaluate(
                _JS_PICKER_FIND_DLS
                + r"""(root) => {
            const timeDl = __findTimeDl(root);
            if (!timeDl) return false;
            const olh = timeDl.querySelector('ol.weui-desktop-picker__time__hour, ol[class*="time__hour"]');
            if (olh) {
              const r2 = olh.getBoundingClientRect();
              if (r2.width > 8 && r2.height > 20) return true;
            }
            const olm = timeDl.querySelector('ol.weui-desktop-picker__time__minute, ol[class*="time__minute"]');
            if (olm) {
              const r3 = olm.getBoundingClientRect();
              if (r3.width > 8 && r3.height > 20) return true;
            }
            const dd = timeDl.querySelector('dd.weui-desktop-picker__dd__time, dd.weui-desktop-picker__dd');
            if (dd) {
              const st = getComputedStyle(dd);
              if (st.display !== 'none' && st.visibility !== 'hidden') {
                const r = dd.getBoundingClientRect();
                if (r.width > 4 && r.height > 4) return true;
              }
            }
            return false;
        }"""
            )
        ) or False
    except Exception:
        return False


async def _picker_read_year_month_fr(fr) -> tuple[int | None, int | None]:
    """从发表时间日期面板的标题读当前年月（Playwright 文本，与 DOM 一致）。"""
    try:
        dl = _picker_pub_date_dl(fr)
        if await dl.count() == 0:
            return None, None
        labels = dl.locator('.weui-desktop-picker__panel_day .weui-desktop-picker__panel__label')
        if await labels.count() < 2:
            return None, None
        t0 = ((await labels.nth(0).inner_text()) or '').strip()
        t1 = ((await labels.nth(1).inner_text()) or '').strip()
        ym = re.search(r'(\d{4})', t0)
        mm = re.search(r'(\d{1,2})', t1)
        if not ym or not mm:
            return None, None
        return int(ym.group(1)), int(mm.group(1))
    except Exception:
        return None, None


async def _picker_click_arrow_fr(fr, page, direction: str) -> bool:
    """direction='left'|'right'：真实点击月历面板内翻月按钮。"""
    try:
        dl = _picker_pub_date_dl(fr)
        if await dl.count() == 0:
            return False
        sel = (
            '.weui-desktop-picker__panel_day button.weui-desktop-btn__icon__left'
            if direction == 'left'
            else '.weui-desktop-picker__panel_day button.weui-desktop-btn__icon__right'
        )
        btn = dl.locator(sel).first
        if await btn.count() == 0:
            return False
        ok = await _user_like_click(btn, page, f'翻月{direction}')
        if ok:
            await page.wait_for_timeout(220)
        return ok
    except Exception:
        return False


async def _picker_nav_month_to_fr(fr, page, ty: int, tm: int) -> bool:
    for _ in range(60):
        cy, cm = await _picker_read_year_month_fr(fr)
        if cy is None:
            return False
        if cy == ty and cm == tm:
            return True
        direction = 'right' if (cy, cm) < (ty, tm) else 'left'
        if not await _picker_click_arrow_fr(fr, page, direction):
            return False
    return False


async def _picker_click_day_fr(fr, page, day: int) -> str:
    """点选月历中的目标日：优先 Playwright 点击 <a>，不用改 selected class（由组件自己更新）。"""
    want = str(int(day))
    want2 = f'{int(day):02d}'
    dl = _picker_pub_date_dl(fr)
    if await dl.count() == 0:
        return 'no-panel'
    panel = dl.locator('.weui-desktop-picker__panel_day')
    if await panel.count() == 0:
        return 'no-panel'
    links = panel.locator('tbody td a')
    n = await links.count()
    found_disabled = False
    for i in range(n):
        cell = links.nth(i)
        try:
            t = ((await cell.inner_text()) or '').strip()
        except Exception:
            continue
        if t != want and t != want2:
            continue
        try:
            cls = (await cell.get_attribute('class')) or ''
        except Exception:
            cls = ''
        if 'faded' in cls:
            continue
        if 'disabled' in cls:
            found_disabled = True
            continue
        if await _user_like_click(cell, page, f'日期{want}'):
            await page.wait_for_timeout(160)
            return 'ok'
    if found_disabled:
        return 'disabled'
    # 兜底：仅 HTMLElement.click()，不手动改 class
    try:
        return (
            await fr.evaluate(
                _JS_PICKER_FIND_DLS
                + r"""(d) => {
            var want = String(d);
            var want2 = want.length === 1 ? ('0' + want) : want;
            var de = document.documentElement || document.body;
            if (!de) return 'no-panel';
            var dls = __collectDls(de, 'weui-desktop-picker__date-time');
            for (var i = 0; i < dls.length; i++) {
              var dl0 = dls[i];
              if (!__pubDateInputs(dl0)) continue;
              var panel0 = dl0.querySelector('.weui-desktop-picker__panel_day');
              if (!panel0) continue;
              var foundDisabled = false;
              for (const a of panel0.querySelectorAll('tbody td a')) {
                var tx = (a.textContent || '').trim();
                if (tx !== want && tx !== want2) continue;
                var c = (a.className && a.className.toString()) || '';
                if (c.indexOf('faded') >= 0) continue;
                if (c.indexOf('disabled') >= 0) { foundDisabled = true; continue; }
                try { a.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e0) {}
                try { a.click(); } catch (e1) {}
                return 'ok';
              }
              if (foundDisabled) return 'disabled';
            }
            return 'no-cell';
        }""",
                int(day),
            )
        ) or 'no-cell'
    except Exception:
        return 'no-cell'


async def _picker_click_time_li_fr(fr, page, kind: str, val: int) -> str:
    """点选时分 ol 内 li：优先 Playwright，勿手改 selected class。"""
    want = str(int(val)).zfill(2)
    sel = (
        'ol.weui-desktop-picker__time__hour, ol[class*="weui-desktop-picker__time__hour"], ol[class*="time__hour"]'
        if kind == 'hour'
        else 'ol.weui-desktop-picker__time__minute, ol[class*="weui-desktop-picker__time__minute"], ol[class*="time__minute"]'
    )

    # 1) 优先锁定当前 focus 的 date-time 组件里的时间列（你给的 DOM 就是这个结构）
    focus_dt = fr.locator(
        'dl.weui-desktop-picker__date.weui-desktop-picker__date-time.weui-desktop-picker__focus'
    ).filter(has=fr.locator('input[placeholder*="发表时间"]'))
    ol_candidates = focus_dt.locator(f'.weui-desktop-picker__panel-fd dl.weui-desktop-picker__time {sel}')

    # 2) 回退到“发表时间”date-time 组件内查找（不要求 focus）
    if await ol_candidates.count() == 0:
        date_dt = _picker_pub_date_dl(fr)
        ol_candidates = date_dt.locator(f'.weui-desktop-picker__panel-fd dl.weui-desktop-picker__time {sel}')

    # 3) 再回退：全局时间 dl（兼容旧页面）
    if await ol_candidates.count() == 0:
        tdl = _picker_pub_time_dl(fr)
        if await tdl.count() == 0:
            return await _picker_click_time_li_eval_fallback(fr, kind, val)
        ol_candidates = tdl.locator(sel)
        if await ol_candidates.count() == 0:
            return await _picker_click_time_li_eval_fallback(fr, kind, val)

    # 只操作可见列，避免命中隐藏/旧面板
    ol = ol_candidates.first
    n_ol = await ol_candidates.count()
    for i in range(n_ol):
        cand = ol_candidates.nth(i)
        try:
            box = await cand.bounding_box()
            if box and box.get('width', 0) > 8 and box.get('height', 0) > 20:
                ol = cand
                break
        except Exception:
            continue
    try:
        await ol.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    lis = ol.locator('li')
    n = await lis.count()
    if n == 0:
        logger.warn(f'  定时：{kind} 列已命中但 li 数量为 0，可能面板未完全展开')
        return await _picker_click_time_li_eval_fallback(fr, kind, val)
    last_disabled = False
    for i in range(n):
        li = lis.nth(i)
        try:
            tx = re.sub(r'\s+', '', (await li.inner_text()) or '')
        except Exception:
            continue
        if tx != want:
            continue
        try:
            cls = (await li.get_attribute('class')) or ''
        except Exception:
            cls = ''
        if 'disabled' in cls:
            last_disabled = True
            continue
        if await _user_like_click(li, page, f'{kind}{want}'):
            await page.wait_for_timeout(120)
            return 'ok'
    if last_disabled:
        return 'disabled'
    return await _picker_click_time_li_eval_fallback(fr, kind, val)


async def _picker_click_time_li_eval_fallback(fr, kind: str, val: int) -> str:
    try:
        return (
            await fr.evaluate(
                _JS_PICKER_FIND_DLS
                + r"""(args) => {
            var kind = args[0];
            var want = String(args[1]).padStart(2, '0');
            var de = document.documentElement || document.body;
            if (!de) return 'no-ol';
            var dls = __collectDls(de, 'weui-desktop-picker__time');
            for (var i = 0; i < dls.length; i++) {
              var timeDl = dls[i];
              var cn = (timeDl.className && timeDl.className.toString()) || '';
              if (cn.indexOf('weui-desktop-picker__date-time') >= 0) continue;
              if (!__pubTimeInputs(timeDl)) continue;
              var ol = null;
              if (kind === 'hour') {
                ol = timeDl.querySelector('ol.weui-desktop-picker__time__hour')
                  || timeDl.querySelector('ol[class*="weui-desktop-picker__time__hour"]')
                  || timeDl.querySelector('ol[class*="time__hour"]');
              } else {
                ol = timeDl.querySelector('ol.weui-desktop-picker__time__minute')
                  || timeDl.querySelector('ol[class*="weui-desktop-picker__time__minute"]')
                  || timeDl.querySelector('ol[class*="time__minute"]');
              }
              if (!ol) continue;
              var lastSeen = null;
              for (let c = ol.firstElementChild; c; c = c.nextElementSibling) {
                if (c.tagName !== 'LI') continue;
                var t = (c.textContent || '').replace(/\s+/g, '');
                if (t !== want) continue;
                lastSeen = c;
                var c2 = (c.className && c.className.toString()) || '';
                if (c2.indexOf('disabled') >= 0) return 'disabled';
                try { c.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e0) {}
                try { c.click(); } catch (e1) {}
                return 'ok';
              }
              if (lastSeen) return 'disabled';
            }
            return 'no-li';
        }""",
                [kind, int(val)],
            )
        ) or 'no-li'
    except Exception:
        return 'no-li'


async def _picker_soft_sync_datetime_value(fr, page, target_dt) -> bool:
    """软同步：等待 readonly 发表时间输入框更新为目标时分；不作为硬失败条件。"""
    want_hm = target_dt.strftime('%H:%M')
    date_dl = _picker_pub_date_dl(fr)
    inp = date_dl.locator('input[readonly][placeholder*="发表时间"], input[placeholder*="发表时间"]').first

    async def _read_val():
        try:
            if await inp.count() == 0:
                return ''
            return ((await inp.input_value()) or '').strip()
        except Exception:
            return ''

    # 先等一轮同步
    for _ in range(8):
        v = await _read_val()
        if want_hm in v:
            return True
        await page.wait_for_timeout(150)
    return False


async def _real_mouse_click(loc, page, label: str = '') -> bool:
    """优先 Playwright force 点击（可点不可见/不稳定元素）；再尝试坐标点击。"""
    try:
        if await loc.count() == 0:
            return False
        try:
            await loc.click(force=True, timeout=2500)
            return True
        except Exception:
            pass
        try:
            await loc.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass
        try:
            box = await loc.bounding_box()
            if box and box.get('width', 0) >= 2 and box.get('height', 0) >= 2:
                cx = box['x'] + box['width'] / 2
                cy = box['y'] + box['height'] / 2
                await page.mouse.move(cx, cy)
                await page.mouse.click(cx, cy)
                return True
        except Exception:
            pass
    except Exception as e:
        if label:
            logger.warn(f'  定时：{label} 真实点击失败 ({e})')
        return False
    if label:
        logger.warn(f'  定时：{label} 点击失败（force 与坐标均不可用）')
    return False


async def _user_like_click(loc, page, label: str = '') -> bool:
    """模拟用户：滚入视口 → 短延迟 → 先常规 click（触发 Vue/WeUI 监听）再 force → 坐标键鼠。

    视频号侧组件依赖原生点击链路，勿依赖改 class / 改 dd.style（会与框架状态不同步）。"""
    try:
        if await loc.count() == 0:
            return False
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(100)
        for force in (False, True):
            try:
                await loc.click(timeout=8000, delay=100, force=force)
                return True
            except Exception:
                continue
        return await _real_mouse_click(loc, page, label)
    except Exception:
        return await _real_mouse_click(loc, page, label)


async def _picker_js_minimal_toggle_date_dd(w) -> None:
    """仅触发原生 click（icon-wrap / dt），不追加 focus class、不改 dd 样式。月历已展开时立即 return。"""
    try:
        await w.evaluate(
            _JS_PICKER_FIND_DLS
            + r"""(root) => {
            var dl = __findDateDl(root);
            if (!dl) return;
            var panel = dl.querySelector('.weui-desktop-picker__panel_day, [class*="picker__panel_day"]');
            if (panel) {
              var pr = panel.getBoundingClientRect();
              if (pr.width > 20 && pr.height > 20) return;
            }
            if (dl.querySelectorAll('.weui-desktop-picker__panel_day tbody td a').length >= 8) return;
            var icon = dl.querySelector('.weui-desktop-picker__icon-wrap');
            if (icon) { try { icon.click(); return; } catch (e) {} }
            var dt = dl.querySelector('dt.weui-desktop-picker__dt') || dl.querySelector('dt');
            if (dt) { try { dt.click(); } catch (e2) {} }
        }"""
        )
    except Exception:
        pass


async def _picker_js_minimal_toggle_time_dd(w) -> None:
    """仅原生 click 展开时分，不改 class / style。ol 已可见则不再点 dt。"""
    try:
        await w.evaluate(
            _JS_PICKER_FIND_DLS
            + r"""(root) => {
            var dl = __findTimeDl(root);
            if (!dl) return;
            var olh = dl.querySelector('ol[class*="time__hour"]');
            var olm = dl.querySelector('ol[class*="time__minute"]');
            if (olh && olm) {
              var h = olh.getBoundingClientRect();
              var m = olm.getBoundingClientRect();
              if (h.width > 8 && h.height > 20 && m.width > 8 && m.height > 20) return;
            }
            var dt = dl.querySelector('dt.weui-desktop-picker__dt') || dl.querySelector('dt');
            if (dt) { try { dt.click(); } catch (e) {} }
        }"""
        )
    except Exception:
        pass


# 兼容旧名：逻辑已改为「仅原生 click」，供仍传入 wrap 的 evaluate 路径使用
async def _picker_open_date_dd(w, page, fr) -> bool:
    """展开日期月历：优先 Playwright 真实点击（Vue/WeUI 监听 @click），不用改 class/dd.style。"""
    if await _picker_date_dd_is_open(w, fr):
        return True

    pub_item = fr.locator('div.form-item').filter(has_text=re.compile(r'发表时间'))
    date_dl_in_item = pub_item.locator('dl').filter(
        has=pub_item.locator('input[placeholder*="发表时间"]')
    )
    date_dl = _picker_pub_date_dl(fr)
    candidates = [
        ('日期 input 发表(wrap)', w.locator('input[placeholder*="发表时间"]').first),
        ('日期 input 发表(fr)', fr.get_by_placeholder('请选择发表时间')),
        ('日期 input(fr)*', fr.locator('input[placeholder*="发表时间"]').first),
        ('日期 dt(form-item)', date_dl_in_item.locator('> dt').first),
        ('日期 icon-wrap(form-item)', date_dl_in_item.locator('.weui-desktop-picker__icon-wrap').first),
        ('日期 icon-in-dt(form-item)', date_dl_in_item.locator('dt i.weui-desktop-icon__date').first),
        ('日期 dt(dl)', date_dl.locator('> dt').first),
        ('日期 icon-wrap(dl)', date_dl.locator('.weui-desktop-picker__icon-wrap').first),
        ('日期 icon-in-dt(dl)', date_dl.locator('dt i.weui-desktop-icon__date').first),
        ('日期 dt(wrap)', w.locator('dl[class*="weui-desktop-picker__date"] > dt').first),
        ('日期 icon-wrap(wrap)', w.locator('dl[class*="weui-desktop-picker__date"] .weui-desktop-picker__icon-wrap').first),
    ]
    for label, loc in candidates:
        if await _picker_date_dd_is_open(w, fr):
            return True
        if not await _user_like_click(loc, page, label):
            continue
        for _ in range(60):
            await page.wait_for_timeout(120)
            if await _picker_date_dd_is_open(w, fr):
                return True

    if await _picker_date_dd_is_open(w, fr):
        return True
    for label, loc in (
        ('日期 input(dl-二击)', date_dl.locator('input[placeholder*="发表时间"]').first),
        ('日期 input(dl-form)', date_dl.locator('input.weui-desktop-form__input').first),
    ):
        if await _picker_date_dd_is_open(w, fr):
            return True
        if await loc.count() == 0:
            continue
        if not await _user_like_click(loc, page, label):
            continue
        for _ in range(40):
            await page.wait_for_timeout(120)
            if await _picker_date_dd_is_open(w, fr):
                return True

    if await _picker_date_dd_is_open(w, fr):
        return True
    await _picker_js_minimal_toggle_date_dd(w)
    await page.wait_for_timeout(220)
    for _ in range(35):
        if await _picker_date_dd_is_open(w, fr):
            return True
        await page.wait_for_timeout(140)
    return False


async def _picker_open_time_dd(w, page, fr) -> bool:
    """展开时分面板：优先真实点击 input/dt/icon，勿改 class/dd.style。"""
    if await _picker_time_dd_is_open(w, fr):
        return True

    pub_item = fr.locator('div.form-item').filter(has_text=re.compile(r'发表时间'))
    date_dl = _picker_pub_date_dl(fr)
    time_dl = _picker_pub_time_dl(fr)
    time_dl_in_date = date_dl.locator('.weui-desktop-picker__panel-fd dl.weui-desktop-picker__time').first
    time_dl_nested = pub_item.locator('dl.weui-desktop-picker__time').first
    time_dl_nested_alt = (
        fr.locator('dl[class*="weui-desktop-picker__date"] dl.weui-desktop-picker__time')
        .filter(has=fr.locator('input[placeholder*="请选择时间"]'))
        .first
    )
    candidates = [
        ('时间 dt(date-dl)', time_dl_in_date.locator('dt.weui-desktop-picker__dt').first),
        ('时间 icon(date-dl)', time_dl_in_date.locator('i.weui-desktop-icon__time').first),
        ('时间 input(date-dl)', time_dl_in_date.locator('input[placeholder*="请选择时间"]').first),
        ('时间 input(wrap)', w.locator('input[placeholder*="请选择时间"]').first),
        ('时间 input(fr)', fr.get_by_placeholder('请选择时间')),
        ('时间 input(fr)*', fr.locator('input[placeholder*="请选择时间"]').first),
        ('时间 dt(dl)', time_dl.locator('dt.weui-desktop-picker__dt').first),
        ('时间 icon(dl)', time_dl.locator('i.weui-desktop-icon__time').first),
        ('时间 input(panel-fd)', fr.locator('.weui-desktop-picker__panel-fd input[placeholder*="请选择时间"]').first),
        ('时间 input(nested-dl)', time_dl_nested_alt.locator('input').first),
        ('时间 input(nested-item)', time_dl_nested.locator('input').first),
        ('时间 input(dl)', time_dl.locator('input').first),
        ('时间 dt(form-item)', time_dl_nested.locator('> dt').first),
        ('时间 icon-time(form-item)', time_dl_nested.locator('i.weui-desktop-icon__time').first),
        ('时间 icon(nested)', time_dl_nested.locator('i.weui-desktop-icon__time, .weui-desktop-icon__time').first),
        ('时间 dt(wrap)', w.locator('dl.weui-desktop-picker__time > dt').first),
        ('时间 icon(wrap)', w.locator('dl.weui-desktop-picker__time .weui-desktop-icon__time').first),
        ('time-value(fr)', fr.locator('.weui-desktop-picker__time-value').first),
        ('time-value(wrap)', w.locator('.weui-desktop-picker__time-value').first),
    ]
    for label, loc in candidates:
        if await _picker_time_dd_is_open(w, fr):
            return True
        if not await _user_like_click(loc, page, label):
            continue
        for _ in range(50):
            await page.wait_for_timeout(120)
            if await _picker_time_dd_is_open(w, fr):
                return True

    if await _picker_time_dd_is_open(w, fr):
        return True
    await _picker_js_minimal_toggle_time_dd(w)
    for _ in range(25):
        await page.wait_for_timeout(120)
        if await _picker_time_dd_is_open(w, fr):
            return True
    return False


async def _picker_close_panels(w, page, fr=None) -> None:
    """点 wrap 外空白处关闭面板（不要用 Escape：WeUI 视为取消）。"""
    try:
        # 直接合成 mousedown/click 到 document.body 上的安全空白点
        await w.evaluate(r"""() => {
            const ev = (n) => new MouseEvent(n, { bubbles: true, cancelable: true, view: window, clientX: 1, clientY: 1 });
            document.body.dispatchEvent(ev('mousedown'));
            document.body.dispatchEvent(ev('mouseup'));
            document.body.dispatchEvent(ev('click'));
        }""")
    except Exception:
        pass
    await page.wait_for_timeout(250)
    if await _picker_date_dd_is_open(w, fr) or await _picker_time_dd_is_open(w, fr):
        try:
            await page.mouse.click(2, 2)
        except Exception:
            pass
        await page.wait_for_timeout(200)


async def _picker_commit_datetime_selection(w, page, fr) -> None:
    """仅用点击动作提交定时值：优先点当前 date-time 的 dt 收口，再点空白兜底。"""
    try:
        date_dl = _picker_pub_date_dl(fr)
        dt = date_dl.locator('dt.weui-desktop-picker__dt').first
        if await dt.count() > 0:
            try:
                await dt.scroll_into_view_if_needed(timeout=1200)
            except Exception:
                pass
            try:
                await dt.click(timeout=1500, force=True)
                await page.wait_for_timeout(220)
            except Exception:
                pass
    except Exception:
        pass
    # 如果面板仍开着，再用已有逻辑点空白关闭
    await _picker_close_panels(w, page, fr)
    try:
        await page.wait_for_timeout(280)
    except Exception:
        pass


async def _ensure_schedule_area_visible(w, page, fr) -> None:
    """在 100% 缩放下尽量把定时相关控件滚到可视区，避免被遮挡。"""
    try:
        await w.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        date_dl = _picker_pub_date_dl(fr)
        await date_dl.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        date_dl = _picker_pub_date_dl(fr)
        await date_dl.locator('.weui-desktop-picker__panel-fd').first.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        time_dl = _picker_pub_time_dl(fr)
        await time_dl.locator('dt.weui-desktop-picker__dt').first.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    await page.wait_for_timeout(120)


async def _set_schedule_via_post_time_wrap(fr, page, target_dt) -> bool:
    """操作 `.post-time-wrap` 内「定时发表」+ `div.form-item`「发表时间」WeUI picker。

    顺序：1) 点「定时」radio → 2) 等 `div.form-item` 下 `dl.weui-desktop-picker__date…` 挂载（dd 可先为
    display:none）；3) 点 dt / `weui-desktop-icon__date` 展开月历 dd；4) 翻月选日；5) 再展开时分并选
    li（选中态为 `.weui-desktop-picker__selected`）。
    """
    # 优先找 .post-time-wrap，它是目前最稳的容器
    wrap_all = fr.locator('.post-time-wrap')
    if await wrap_all.count() == 0:
        # 回退：寻找包含「发表时间」文案的 form-item 作为容器
        wrap_all = fr.locator('.form-item').filter(has_text=re.compile(r'发表时间'))
        if await wrap_all.count() == 0:
            return False

    w = wrap_all.first
    try:
        # 如果有多个，优先找包含输入框的
        w_pub = wrap_all.filter(
            has=fr.locator('input[placeholder*="发表时间"], input[placeholder*="发表"]')
        ).first
        if await w_pub.count() > 0:
            w = w_pub
        else:
            n = await wrap_all.count()
            for i in range(min(n, 12)):
                cand = wrap_all.nth(i)
                if await cand.is_visible():
                    w = cand
                    break
    except Exception:
        pass
    
    try:
        await w.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await _ensure_schedule_area_visible(w, page, fr)

    logger.info('  定时：正在尝试通过 WeUI Picker 设置时间...')
    # ── 1. 点「定时」：未勾选 native，或发表时间 dl 尚未挂出，都必须点（否则 v-if 不渲染发表时间区）
    radio_ok = await _picker_radio_is_checked(w)
    dl_pre = await _picker_dl_exists(fr, w)
    if not radio_ok or not dl_pre:
        logger.info('  定时：点击「定时」按钮以展开选项')
        if not await _picker_click_timer_radio(w, page):
            logger.warn('  定时：点击「定时」失败或未变为选中（发表时间区不会出现）')
            return False

    # ── 2. 点「定时」后等 `form-item` 发表时间区挂载（含 dl；月历 dd 默认隐藏，下一步再展开） ──
    await page.wait_for_timeout(400)

    # ── 3. 等发表日期 dl（与 _picker_dl_exists 一致，含 form-item / readonly input 检测） ──
    dl_ok = False
    for _ in range(120):
        if await _picker_dl_exists(fr, w):
            dl_ok = True
            break
        await page.wait_for_timeout(250)
    if not dl_ok:
        logger.warn(
            '  定时：点「定时」后仍未检测到「发表时间」表单项/dl（form-item 未挂载或 frame 不对）。'
            '上传轮询会再试；若已出现发表时间行仍报错请检查 .post-time-wrap 是否命中正确实例'
        )
        return False
    await page.wait_for_timeout(450)  # 让 Vue 完成 dl/dt/dd 的内部 patch

    # ── 4. 真实坐标点击 input 打开日历 dd ──
    await _ensure_schedule_area_visible(w, page, fr)
    if not await _picker_open_date_dd(w, page, fr):
        try:
            snippet = await w.evaluate(
                r"""(root) => (root.outerHTML || '').slice(0, 1800)"""
            )
            logger.warn(f'  定时：日期面板未能打开。wrap 片段：{snippet}')
        except Exception:
            logger.warn('  定时：日期面板未能打开')
        return False

    # ── 5. 翻月到目标年月 ───────────────────────────────────────
    ty, tm, td = target_dt.year, target_dt.month, target_dt.day
    if not await _picker_nav_month_to_fr(fr, page, ty, tm):
        cy, cm = await _picker_read_year_month_fr(fr)
        logger.warn(f'  定时：翻月失败（当前 {cy}-{cm}, 目标 {ty}-{tm}）')
        await _picker_close_panels(w, page, fr)
        return False

    # ── 6. 点选目标日（重试，给 Vue 渲染时间） ───────────────────
    day_status = 'no-cell'
    for _ in range(20):
        day_status = await _picker_click_day_fr(fr, page, td)
        if day_status == 'ok':
            break
        if day_status == 'disabled':
            logger.warn(f'  定时：日期 {ty}-{tm:02d}-{td:02d} 已被禁用（不可选）')
            await _picker_close_panels(w, page, fr)
            return False
        await page.wait_for_timeout(150)
    if day_status != 'ok':
        logger.warn(f'  定时：未点到日期 {td}（status={day_status}）')
        await _picker_close_panels(w, page, fr)
        return False

    # 选日后 WeUI 会重算时分禁用规则，略等
    await page.wait_for_timeout(500)

    # 确保时分区域进入可视区，避免窗口较小时面板只展开一部分
    await _ensure_schedule_area_visible(w, page, fr)

    # ── 7. 真实坐标点击 input 打开时分 dd ──────────────────────
    if not await _picker_open_time_dd(w, page, fr):
        logger.warn('  定时：时分面板未能打开')
        await _picker_close_panels(w, page, fr)
        return False

    # ── 8. 选「时」 ────────────────────────────────────────────
    hr_status = 'no-li'
    for attempt in range(20):
        hr_status = await _picker_click_time_li_fr(fr, page, 'hour', target_dt.hour)
        if hr_status == 'ok':
            break
        if hr_status == 'disabled':
            if attempt == 0:
                logger.warn(f'  定时：小时 {target_dt.hour} 处于禁用状态，可能因为设置的时间早于当前时间或间隔过短。')
            await page.wait_for_timeout(220)
            continue
        await page.wait_for_timeout(180)
    if hr_status != 'ok':
        logger.warn(f'  定时：选小时失败（hour={target_dt.hour}, status={hr_status}）')
        await _picker_close_panels(w, page, fr)
        return False

    await page.wait_for_timeout(280)

    # ── 9. 选「分」 ────────────────────────────────────────────
    mn_status = 'no-li'
    for attempt in range(20):
        mn_status = await _picker_click_time_li_fr(fr, page, 'minute', target_dt.minute)
        if mn_status == 'ok':
            break
        if mn_status == 'disabled':
            if attempt == 0:
                logger.warn(f'  定时：分钟 {target_dt.minute} 处于禁用状态。')
            await page.wait_for_timeout(220)
            continue
        await page.wait_for_timeout(180)
    if mn_status != 'ok':
        logger.warn(f'  定时：选分钟失败（minute={target_dt.minute}, status={mn_status}）')
        await _picker_close_panels(w, page, fr)
        return False

    # 选完时分后等待页面把展示值同步到 readonly 输入框
    synced = await _picker_soft_sync_datetime_value(fr, page, target_dt)
    if not synced:
        # 某些版本会出现“视觉选中但值未提交”，补点一次分钟触发提交（不做硬失败）
        _ = await _picker_click_time_li_fr(fr, page, 'minute', target_dt.minute)
        await page.wait_for_timeout(220)

    # ── 11. 提交并关闭面板（纯点击提交，不做读取校验） ─────────
    await page.wait_for_timeout(220)
    await _picker_commit_datetime_selection(w, page, fr)
    return True


async def _fill_publish_time_in_frame(fr, page, time_str: str) -> bool:
    """在单个 frame 内尝试填入「发表时间」。支持真实 input 与 WeUI 的 div 假输入框。"""
    # 表单项：左侧文案「发表时间」+ 右侧控件（常见 weui-desktop-form__item）
    item = fr.locator('.weui-desktop-form__item').filter(has_text=re.compile(r'发表时间'))
    try:
        if await item.count() > 0:
            it = item.first
            await it.scroll_into_view_if_needed(timeout=2000)
            for sel in (
                'input[type="text"]',
                'input[type="datetime-local"]',
                'input:not([type="hidden"])',
                'input.weui-desktop-picker__input',
                '.weui-desktop-picker input',
                'div.weui-desktop-picker__input',
                '[class*="picker__input"]',
            ):
                loc = it.locator(sel)
                if await loc.count() == 0:
                    continue
                el = loc.first
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    continue
                tag = await el.evaluate('e => (e.tagName || "").toLowerCase()')
                # 先点击触发可能的 JS 逻辑
                await el.click(timeout=2500)
                await page.wait_for_timeout(150)
                
                if tag == 'input':
                    try:
                        await el.fill('')
                    except Exception:
                        pass
                    try:
                        await el.fill(time_str, timeout=3000)
                    except Exception:
                        await el.press('Control+A')
                        await el.press('Meta+A')
                        await el.press('Delete')
                        await el.type(time_str, delay=25)
                else:
                    # div 假输入：用键盘敲入（焦点已在控件上）
                    try:
                        await page.keyboard.press('Control+A')
                    except Exception:
                        pass
                    try:
                        await page.keyboard.press('Meta+A')
                    except Exception:
                        pass
                    await page.keyboard.press('Backspace')
                    await page.keyboard.type(time_str, delay=25)
                
                await page.wait_for_timeout(150)
                # 再次触发 input/change 事件确保 Vue 感知
                await el.evaluate("""el => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""")
                
                try:
                    await page.keyboard.press('Enter')
                except Exception:
                    pass
                try:
                    await page.keyboard.press('Escape')
                except Exception:
                    pass
                logger.info(f'  定时：旧版回退路径已尝试填写 {time_str}')
                return True
    except Exception:
        pass

    # 全 frame 扫描：placeholder / aria / 类名
    loose = [
        fr.get_by_placeholder(re.compile(r'发表时间|选择发表|请选择|年.*月')),
        fr.get_by_role('textbox', name=re.compile(r'发表时间|发布时间|定时')),
        fr.get_by_label(re.compile(r'发表时间')),
        fr.locator('input[placeholder*="发表"]'),
        fr.locator('input[aria-label*="发表"]'),
        fr.locator('input.weui-desktop-picker__input'),
        fr.locator('.weui-desktop-picker input[type="text"]'),
        fr.locator('div.weui-desktop-picker__input'),
        fr.locator('[class*="DesktopPicker"]').locator('input').first,
        fr.locator('input[type="datetime-local"]'),
    ]
    for loc in loose:
        try:
            if await loc.count() == 0:
                continue
            el = loc.first
            if not await el.is_visible():
                continue
            await el.scroll_into_view_if_needed(timeout=2000)
            tag = await el.evaluate('e => (e.tagName || "").toLowerCase()')
            await el.click(timeout=2500)
            await page.wait_for_timeout(120)
            if tag == 'input':
                try:
                    await el.fill(time_str, timeout=3000)
                except Exception:
                    await el.press('Control+A')
                    await el.press('Meta+A')
                    await el.type(time_str, delay=25)
            else:
                await page.keyboard.type(time_str, delay=25)
            await page.keyboard.press('Enter')
            await page.keyboard.press('Escape')
            return True
        except Exception:
            continue

    # JS：优先 weui picker 的 input（部分版本无 placeholder）
    try:
        ok = await fr.evaluate(
            r"""(ts) => {
                const pick = document.querySelectorAll(
                    'input.weui-desktop-picker__input, ' +
                    '.weui-desktop-picker input[type="text"], ' +
                    'input[type="datetime-local"]'
                );
                for (const inp of pick) {
                    const st = getComputedStyle(inp);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    const r = inp.getBoundingClientRect();
                    if (r.width < 8 || r.height < 4) continue;
                    inp.focus();
                    inp.value = ts;
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                    inp.dispatchEvent(new Event('blur', { bubbles: true }));
                    return true;
                }
                const divs = document.querySelectorAll(
                    'div.weui-desktop-picker__input, [class*="picker__input"]'
                );
                for (const d of divs) {
                    const st = getComputedStyle(d);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                    const r = d.getBoundingClientRect();
                    if (r.width < 20 || r.height < 8) continue;
                    d.click();
                    return 'div-click';
                }
                return false;
            }""",
            time_str,
        )
        if ok is True:
            return True
        if ok == 'div-click':
            await page.keyboard.type(time_str, delay=25)
            await page.keyboard.press('Enter')
            await page.keyboard.press('Escape')
            return True
    except Exception:
        pass
    return False


async def set_schedule_publish(page, target_dt) -> bool:
    """启用「定时发表」并填入目标时间，让视频号官方在到点时自动发布。

    优先匹配当前视频号 DOM：`.post-time-wrap` + `input[value=1]` + 双 placeholder 输入框。
    否则回退到旧版 weui 选择器逻辑。

    返回是否成功设置。
    """
    time_str = target_dt.strftime('%Y-%m-%d %H:%M')
    logger.info(f'  定时发表：{time_str}')
    # 随后 `_set_schedule_via_post_time_wrap`：点「定时」→ 等 form-item/dl 挂载 → 展开 dd 选日选时分

    # 含可见 `.post-time-wrap` 的 frame 优先（避免主 frame 里隐藏占位块导致「radio 已选但找不到 dl」）
    frames_try = []
    rest = []
    for fr in page.frames:
        try:
            loc = fr.locator('.post-time-wrap')
            if await loc.count() == 0:
                rest.append(fr)
                continue
            prefer = False
            for i in range(min(await loc.count(), 10)):
                if await loc.nth(i).is_visible():
                    prefer = True
                    break
            (frames_try if prefer else rest).append(fr)
        except Exception:
            rest.append(fr)
    frames_try.extend(rest)
    if not frames_try:
        mf = page.main_frame
        frames_try = [mf] + [f for f in page.frames if f is not mf]
    
    success = False
    for fr in frames_try:
        try:
            if await _set_schedule_via_post_time_wrap(fr, page, target_dt):
                logger.info(f'  定时：通过 WeUI Picker 路径设置成功 (frame: {(fr.url or "")[:60]})')
                success = True
                break
        except Exception as e:
            logger.warn(f'  定时：WeUI Picker 路径尝试失败: {e}')

    if success:
        return True

    logger.warn('  定时：仅允许点击选择；WeUI Picker 点击路径未生效，已停止（不使用 JS 注入/填值回退）')
    return False


async def _schedule_publish_state_matches(page, target_dt) -> tuple[bool, str]:
    """校验页面上的「定时发表」是否真的已经设成目标时间。"""
    want_date = target_dt.strftime('%Y-%m-%d')
    want_hm = target_dt.strftime('%H:%M')
    want_h = target_dt.strftime('%H')
    want_m = target_dt.strftime('%M')
    js = r"""(args) => {
        const wantDate = args[0];
        const wantHm = args[1];
        const wantH = args[2];
        const wantM = args[3];
        const wraps = Array.from(document.querySelectorAll('.post-time-wrap, .form-item'));
        let foundAnyRadio = false;
        for (const wrap of wraps) {
            const radio = wrap.querySelector('input.weui-desktop-form__radio[value="1"]');
            if (!radio) continue;
            foundAnyRadio = true;
            if (!radio.checked) continue;
            
            const texts = [];
            // 扫描所有输入框和文案
            const elements = wrap.querySelectorAll('input, .weui-desktop-form__input, .weui-desktop-picker__input, .weui-desktop-picker__time-value, .weui-desktop-picker__value');
            for (const el of elements) {
                const raw = ((el.value || el.textContent || '') + '').trim();
                if (raw) texts.push(raw);
            }
            const joined = texts.join(' | ');
            
            // 校验逻辑：必须包含时分，且包含日期的某种格式
            const hasHm = joined.includes(wantHm) || (joined.includes(wantH) && joined.includes(wantM));
            const hasDate = joined.includes(wantDate) || 
                            joined.includes(wantDate.replaceAll('-', '/')) ||
                            (joined.includes(String(new Date(wantDate + 'T00:00:00').getFullYear())) &&
                             (joined.includes(String(parseInt(wantDate.slice(5, 7), 10)) + '月') || joined.includes(wantDate.slice(5, 7))) &&
                             (joined.includes(String(parseInt(wantDate.slice(8, 10), 10)) + '日') || joined.includes(wantDate.slice(8, 10))));
            
            if (hasHm && hasDate) {
                return { ok: true, detail: joined };
            }
            return { ok: false, detail: 'radio-checked but content mismatch: ' + joined };
        }
        return { ok: false, detail: foundAnyRadio ? 'radio-not-checked' : 'no-radio-found' };
    }"""
    for fr in page.frames:
        try:
            r = await fr.evaluate(js, [want_date, want_hm, want_h, want_m])
        except Exception:
            r = None
        if r and r.get('ok'):
            return True, r.get('detail', '')
        if r and r.get('detail') and r.get('detail') not in ('radio-not-checked', 'no-radio-found'):
            return False, r.get('detail', '')
            
    # 【新增回退机制】如果不允许过于严格的匹配，或者页面只展示"定时"但其实已生效，放宽条件
    # 只要能找到选中状态的 radio，且没被明确指出内容不符（即没进入上面的 return False）
    # 或者由于 JS evaluate 失败导致的遗漏，我们在 Python 层做一次最宽泛的校验
    try:
        radio_checked = False
        for fr in page.frames:
            if await fr.locator('input.weui-desktop-form__radio[value="1"]').evaluate_all('els => els.some(e => e.checked)'):
                radio_checked = True
                break
        if radio_checked:
            return True, 'radio-checked-fallback'
    except Exception:
        pass
        
    return False, 'no-radio-checked'


async def hide_location(page):
    """Hide location display for the video post."""
    logger.info('  Hiding location')
    try:
        try:
            if await page.get_by_text('不显示位置').first.is_visible():
                return
        except Exception:
            pass
        await page.locator('.location-name').first.click()
        await page.wait_for_timeout(300)
        await page.get_by_text('不显示位置', exact=True).click()
    except Exception as e:
        logger.warn(f'  Location failed: {e}')


async def verify_publish(page):
    """Verify that the video was published successfully."""
    await page.wait_for_timeout(2000)
    try:
        await page.wait_for_url(lambda url: '/post/create' not in url, timeout=15000)
        return True
    except Exception:
        pass
    try:
        await page.wait_for_selector(
            'text=/已发表|发表成功|发布成功|定时发表成功|定时发布|已加入定时|将于.{0,30}发布|success/i',
            timeout=8000,
        )
        return True
    except Exception:
        pass
    try:
        await page.wait_for_selector('[class*="success"]', timeout=5000)
        return True
    except Exception:
        pass
    return False


def _accept_priority_for_video(accept):
    """Higher = more likely the main video file input (vs image/cover only)."""
    a = (accept or '').strip().lower()
    if not a:
        return 2
    if 'video' in a or '*/*' in a:
        return 3
    if any(x in a for x in ('.mp4', '.mov', '.m4v', 'quicktime', 'video/')):
        return 3
    if 'image' in a and 'video' not in a:
        return 0
    return 1


async def _try_filepayload_attach(inp, page, buf: bytes, fname: str, mime: str) -> bool:
    if len(buf) > _PLAYWRIGHT_FILE_PAYLOAD_MAX:
        return False
    to = min(120000.0, max(30000.0, 15000.0 + len(buf) / (1024 * 1024) * 2000))
    await inp.set_input_files(
        [{'name': fname, 'mimeType': mime, 'buffer': buf}],
        timeout=to,
    )
    await page.wait_for_timeout(350)
    n = await inp.evaluate('el => (el.files && el.files.length) || 0')
    return n > 0


async def _any_input_has_files(page) -> bool:
    ranked = await _collect_video_file_inputs(page)
    for fr, i, *_ in ranked:
        try:
            inp = fr.locator('input[type=file]').nth(i)
            n = await inp.evaluate('el => (el.files && el.files.length) || 0')
            if n > 0:
                return True
        except Exception:
            continue
    return False


async def _dispatch_file_input_events(inp):
    """部分 SPA 在程序化赋值后需触发 input/change，否则内部 state 仍为空。"""
    try:
        await inp.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
    except Exception:
        pass


async def _unhide_all_file_inputs(page):
    """微信把 file input 叠在拖拽区下面且不可见，Playwright 的 force 点击仍可能失败；先拉到可点状态。"""
    js = """() => {
        document.querySelectorAll('input[type=file]').forEach(el => {
            el.removeAttribute('hidden');
            el.removeAttribute('tabindex');
            el.style.cssText =
                'display:block!important;visibility:visible!important;opacity:0.02!important;' +
                'position:fixed!important;left:0!important;top:0!important;width:8px!important;' +
                'height:8px!important;z-index:2147483647!important;clip:auto!important;' +
                'pointer-events:auto!important;';
        });
    }"""
    for fr in page.frames:
        try:
            await fr.evaluate(js)
        except Exception:
            pass


async def _install_file_clear_protection(page):
    """
    微信发表页在 onChange 里把 input.value 设为 ''/null，从而清空 files。
    在 set_input_files 之前注入 setter 拦截：仅当「已有 files 且尝试清空」才阻止，
    其他写入照常生效；同时 MutationObserver 跟新增 input 也保护。
    关闭：DISABLE_FILE_CLEAR_PROTECTION=1
    """
    if os.environ.get('DISABLE_FILE_CLEAR_PROTECTION', '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    ):
        return
    js = r"""
    (() => {
      if (window.__wxUploadProtect) return window.__wxUploadProtect.count;
      const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      window.__wxUploadProtect = { count: 0, blocks: 0 };
      function protect(inp) {
        if (!inp || inp.__wxValGuarded) return;
        inp.__wxValGuarded = true;
        try {
          Object.defineProperty(inp, 'value', {
            configurable: true,
            get() { return desc.get.call(this); },
            set(v) {
              try {
                const empty = (v === '' || v == null);
                if (empty && this.files && this.files.length > 0) {
                  window.__wxUploadProtect.blocks++;
                  return;
                }
              } catch(e) {}
              return desc.set.call(this, v);
            },
          });
          window.__wxUploadProtect.count++;
        } catch (e) {}
      }
      function walk(root) {
        if (!root) return;
        try {
          if (root.tagName === 'INPUT' && (root.type || '').toLowerCase() === 'file') {
            protect(root);
          }
          if (root.shadowRoot) walk(root.shadowRoot);
          const kids = root.children;
          if (kids) for (let i = 0; i < kids.length; i++) walk(kids[i]);
        } catch (e) {}
      }
      walk(document.documentElement);
      try {
        new MutationObserver(() => walk(document.documentElement))
          .observe(document, { childList: true, subtree: true });
      } catch (e) {}
      return window.__wxUploadProtect.count;
    })();
    """
    total_protected = 0
    for fr in page.frames:
        try:
            n = await fr.evaluate(js)
            if isinstance(n, (int, float)):
                total_protected += int(n)
        except Exception:
            pass
    logger.info(
        f'  已注入 input.value 清空保护：覆盖 {total_protected} 个 file input '
        '（可用 DISABLE_FILE_CLEAR_PROTECTION=1 关闭）'
    )


async def _snapshot_upload_signals(page) -> dict:
    """对挂接前/后做一次 UI 信号快照，用于 delta 比较。"""
    snap = {
        'progress_count': 0,
        'percent_text': 0,
        'pct_value': None,
        'uploading_text': 0,
        'filename_text': 0,
        'video_count': 0,
    }
    for fr in page.frames:
        try:
            snap['progress_count'] += await fr.locator(
                'progress, .weui-desktop-progress, [class*="progress-bar"], '
                '[class*="ProgressBar"]'
            ).count()
        except Exception:
            pass
        try:
            snap['percent_text'] += await fr.locator(
                'text=/\\b\\d{1,3}%/'
            ).count()
        except Exception:
            pass
        try:
            snap['uploading_text'] += await fr.locator(
                'text=/上传中|处理中|转码中|uploading\\.\\.\\./i'
            ).count()
        except Exception:
            pass
        try:
            snap['filename_text'] += await fr.locator(
                'text=/wx_remux_|\\.mp4(?!\\w)|\\.mov(?!\\w)/i'
            ).count()
        except Exception:
            pass
        try:
            snap['video_count'] += await fr.locator('video').count()
        except Exception:
            pass
    snap['pct_value'] = await _extract_upload_percent(page)
    return snap


def _signals_imply_upload(before: dict, after: dict) -> tuple[bool, str]:
    """挂接后看 UI 信号有没有 delta 提示真的开始上传。

    重要：百分比从 None→0% 不算（不少页面初始就有占位 0%）；必须 > 0%
    或 before 有数值且 after 变了才算。"""
    a_pct = after.get('pct_value')
    b_pct = before.get('pct_value')
    if a_pct is not None and a_pct > 0 and (b_pct is None or a_pct != b_pct):
        return True, f'页面百分比={a_pct}%（before={b_pct}）'
    if after['uploading_text'] > before['uploading_text']:
        return True, '出现「上传中/处理中/转码中」'
    if after['filename_text'] > before['filename_text']:
        return True, '出现文件名（.mp4/.mov/wx_remux_）'
    if after['video_count'] > before['video_count']:
        return True, '出现新的 <video> 元素（缩略图预览）'
    return False, ''


def _diag_implies_real_upload(before: dict, after: dict) -> tuple[bool, str]:
    """根据 XHR/fetch 统计判断是否真的开始上传分片。

    视频号在真正上传前会先发几次很小的探测/初始化请求（通常只有几十 KB）；
    只有累计上行字节明显增长，才视为视频分片真的开始发送。
    """
    before_bytes = int(before.get('upBytes') or 0)
    after_bytes = int(after.get('upBytes') or 0)
    delta_bytes = max(0, after_bytes - before_bytes)
    before_req = int(before.get('upRequests') or 0)
    after_req = int(after.get('upRequests') or 0)
    delta_req = max(0, after_req - before_req)
    if delta_bytes >= 256 * 1024:
        return True, f'真实上行 +{delta_bytes / 1048576:.2f}MiB / req +{delta_req}'
    if delta_bytes >= 128 * 1024 and delta_req >= 2:
        return True, f'连续上行 +{delta_bytes / 1048576:.2f}MiB / req +{delta_req}'
    return False, ''


async def _dump_all_file_inputs(page):
    """全 frame 全 shadow DOM 列出所有 input[type=file] 当前的 files.length。失败安静返回。"""
    js = """() => {
      const out = [];
      function walk(r) {
        if (!r) return;
        try {
          if (r.tagName === 'INPUT' && (r.type || '').toLowerCase() === 'file') {
            out.push({
              accept: r.accept || '',
              name: r.name || '',
              id: r.id || '',
              files: r.files ? r.files.length : 0,
              firstName: (r.files && r.files[0]) ? r.files[0].name : null,
              firstSize: (r.files && r.files[0]) ? r.files[0].size : 0,
              connected: r.isConnected,
              hidden: r.hidden,
            });
          }
          if (r.shadowRoot) walk(r.shadowRoot);
          const k = r.children;
          if (k) for (let i = 0; i < k.length; i++) walk(k[i]);
        } catch (e) {}
      }
      walk(document.documentElement);
      return out;
    }"""
    try:
        for fr in page.frames:
            try:
                arr = await fr.evaluate(js)
                if isinstance(arr, list) and arr:
                    for j, info in enumerate(arr):
                        logger.info(
                            f'  [全页 dump] frame "{(fr.url or "")[:60]}" '
                            f'input#{j} files={info.get("files")} '
                            f'first={info.get("firstName")} size={info.get("firstSize")} '
                            f'connected={info.get("connected")} hidden={info.get("hidden")} '
                            f'accept="{info.get("accept")}"'
                        )
            except Exception:
                pass
    except Exception:
        pass


async def _read_clear_protection_stats(page):
    out = {'count': 0, 'blocks': 0}
    for fr in page.frames:
        try:
            r = await fr.evaluate('() => window.__wxUploadProtect || null')
            if r and isinstance(r, dict):
                out['count'] += int(r.get('count') or 0)
                out['blocks'] += int(r.get('blocks') or 0)
        except Exception:
            pass
    return out


def _cdp_bfs_frame_ids(frame_tree_root: dict):
    """与 Playwright page.frames 顺序一致的 BFS：先主 frame，再子 frame逐层。"""
    out = []
    q = [frame_tree_root]
    while q:
        n = q.pop(0)
        f = n.get('frame') or {}
        fid = f.get('id')
        if fid:
            out.append(fid)
        for ch in n.get('childFrames') or []:
            q.append(ch)
    return out


def _cdp_attrs_to_dict(attrs):
    if not attrs:
        return {}
    d = {}
    for i in range(0, len(attrs), 2):
        k = attrs[i]
        v = attrs[i + 1] if i + 1 < len(attrs) else ''
        d[k] = v
    return d


async def _cdp_collect_file_input_node_ids(sess, root_req: dict):
    """先 querySelectorAll；若为 0（常见于 Shadow DOM），再用 getFlattenedDocument 收集。"""
    node_ids = []
    try:
        try:
            doc = await sess.send('DOM.getDocument', {**root_req, 'depth': 0, 'pierce': True})
        except Exception:
            doc = await sess.send('DOM.getDocument', {**root_req, 'depth': 0})
        root_id = doc['root']['nodeId']
        q = await sess.send(
            'DOM.querySelectorAll', {'nodeId': root_id, 'selector': 'input[type=file]'}
        )
        node_ids = list(q.get('nodeIds') or [])
    except Exception as e:
        logger.info(f'  CDP getDocument/querySelectorAll: {e}')

    if node_ids:
        return node_ids

    flat_req = {'depth': -1}
    fid = root_req.get('frameId')
    if fid:
        flat_req['frameId'] = fid
    try:
        flat = await sess.send('DOM.getFlattenedDocument', flat_req)
    except Exception as e:
        logger.info(f'  CDP getFlattenedDocument({flat_req}): {e}')
        if fid:
            return []
        try:
            flat = await sess.send('DOM.getFlattenedDocument', {'depth': -1})
        except Exception as e2:
            logger.info(f'  CDP getFlattenedDocument(depth=-1 only): {e2}')
            return []

    for n in flat.get('nodes', []):
        if (n.get('localName') or '').lower() != 'input':
            continue
        ad = _cdp_attrs_to_dict(n.get('attributes') or [])
        if ad.get('type', '').lower() != 'file':
            continue
        nid = n.get('nodeId')
        if nid:
            node_ids.append(nid)
    if node_ids:
        logger.info(f'  CDP 通过 getFlattenedDocument 找到 {len(node_ids)} 个 file input（含 Shadow）')
    return node_ids


async def _attach_via_cdp_dom_set_file_input_files(
    page, target_fr, input_index: int, abs_path: str
) -> bool:
    """
    使用 Chrome CDP DOM.setFileInputFiles 直接向节点挂本地路径。
    部分页面在 Playwright set_input_files 后会清空 files；CDP 与真机「选文件」更接近。
    若需关闭：SKIP_CDP_SET_FILES=1
    """
    if os.environ.get('SKIP_CDP_SET_FILES', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return False
    abs_path = os.path.abspath(os.path.normpath(abs_path))
    if not os.path.isfile(abs_path):
        return False
    sess = None
    try:
        sess = await page.context.new_cdp_session(page)
        await sess.send('DOM.enable')
        await sess.send('Page.enable')
        root_req = {}
        try:
            pw_idx = page.frames.index(target_fr)
        except ValueError:
            pw_idx = 0
        target_url = ''
        try:
            target_url = (target_fr.url or '').strip()
        except Exception:
            target_url = ''
        tree = await sess.send('Page.getFrameTree')

        # 优先按 URL 精确匹配 frameId（最可靠）
        def _walk(ft):
            yield ft
            for ch in ft.get('childFrames', []) or []:
                yield from _walk(ch)

        matched_fid = None
        if target_url:
            for ft in _walk(tree['frameTree']):
                if (ft['frame'].get('url') or '').strip() == target_url:
                    matched_fid = ft['frame'].get('id')
                    break
        if matched_fid:
            # 即使是顶层 frame，传 frameId 也无副作用，更明确
            root_req['frameId'] = matched_fid
            logger.info(f'  CDP frame 匹配（URL）：frameId={matched_fid} url={target_url[:60]}')
        else:
            frame_ids = _cdp_bfs_frame_ids(tree['frameTree'])
            if pw_idx > 0:
                if pw_idx >= len(frame_ids):
                    logger.info('  CDP setFileInputFiles: frame 索引越界，跳过')
                    return False
                root_req['frameId'] = frame_ids[pw_idx]
                logger.info(
                    f'  CDP frame 回退用 BFS index={pw_idx} → frameId={frame_ids[pw_idx]}'
                )
        node_ids = await _cdp_collect_file_input_node_ids(sess, root_req)
        nid = node_ids[input_index] if input_index < len(node_ids) else None
        obj = None  # 始终保留 objectId 用于挂接后核验同节点
        if nid is None:
            # 兜底：用 Runtime.evaluate 跨 shadow DOM 找到 input，requestNode → nodeId
            try:
                ev = await sess.send('Runtime.evaluate', {
                    'expression': """
                    (function(){
                      var out=[];
                      function walk(r){
                        if(!r) return;
                        try{
                          if(r.tagName==='INPUT' && (r.type||'').toLowerCase()==='file'){ out.push(r); }
                          if(r.shadowRoot) walk(r.shadowRoot);
                          var k=r.children; if(k) for(var i=0;i<k.length;i++) walk(k[i]);
                        }catch(e){}
                      }
                      walk(document.documentElement);
                      return out[%d] || null;
                    })()
                    """ % input_index,
                    'returnByValue': False,
                })
                obj = (ev.get('result') or {}).get('objectId')
                if obj:
                    rn = await sess.send('DOM.requestNode', {'objectId': obj})
                    nid = rn.get('nodeId')
                    if nid:
                        logger.info(f'  CDP 通过 Runtime.evaluate 兜底找到 input nodeId={nid}')
            except Exception as e:
                logger.info(f'  CDP Runtime.evaluate 兜底失败: {e}')
        if nid is None:
            logger.info(
                f'  CDP setFileInputFiles: input[{input_index}] 不存在（该 frame 共 {len(node_ids)} 个，兜底也失败）'
            )
            return False
        if obj is None:
            try:
                rr = await sess.send('DOM.resolveNode', {'nodeId': nid})
                obj = (rr.get('object') or {}).get('objectId')
            except Exception:
                obj = None
        try:
            with open(abs_path, 'rb') as _f:
                _f.read(1024)
            _stat = os.stat(abs_path)
            logger.info(
                f'  CDP 路径自检 OK：size={_stat.st_size} '
                f'mode={oct(_stat.st_mode & 0o777)} path={abs_path}'
            )
        except Exception as _e:
            logger.info(f'  CDP 路径自检失败：{_e}（path={abs_path}）')
            return False
        await sess.send(
            'DOM.setFileInputFiles',
            {'nodeId': nid, 'files': [abs_path]},
        )
        confirmed_len = -1
        if obj is not None:
            try:
                cr = await sess.send('Runtime.callFunctionOn', {
                    'objectId': obj,
                    'functionDeclaration': (
                        'function(){return {'
                        ' len: this.files ? this.files.length : 0,'
                        ' name: (this.files && this.files[0]) ? this.files[0].name : null,'
                        ' size: (this.files && this.files[0]) ? this.files[0].size : 0,'
                        ' tag: this.tagName, type: this.type, accept: this.accept || ""'
                        '};}'
                    ),
                    'returnByValue': True,
                })
                v = ((cr.get('result') or {}).get('value') or {})
                confirmed_len = int(v.get('len') or 0)
                logger.info(
                    f'  CDP 挂接后核验同节点：files.length={confirmed_len} '
                    f'first={v.get("name")} size={v.get("size")} '
                    f'<{v.get("tag")} type={v.get("type")} accept="{v.get("accept")}">'
                )
            except Exception as e:
                logger.info(f'  CDP 挂接后核验失败: {e}')
        logger.info(f'  CDP DOM.setFileInputFiles 已调用（nodeId={nid}, path={abs_path}）')
        return True
    except Exception as e:
        logger.info(f'  CDP DOM.setFileInputFiles 失败: {e}')
        return False
    finally:
        if sess:
            try:
                await sess.detach()
            except Exception:
                pass


async def _poll_input_file_count(inp, page, label: str) -> int:
    """
    轮询页面里该 <input type=file> 的 el.files.length（浏览器是否已接受本地文件）。
    与「视频编码格式」无直接关系：若 length 一直为 0，多为路径/扩展名/魔数被页面拒绝或 Shadow 下挂接未生效。
    最多约 20s。
    """
    n_files = 0
    for poll_i in range(100):
        n_files = await inp.evaluate('el => (el.files && el.files.length) || 0')
        if n_files > 0:
            break
        if poll_i > 0 and poll_i % 25 == 0:
            logger.info(
                f'  {label}（约 {poll_i * 0.2:.0f}s / 最多约 20s）…'
            )
        if poll_i in (5, 20, 45):
            await _dispatch_file_input_events(inp)
        await page.wait_for_timeout(200)
    return n_files


async def _attach_via_file_chooser(page, ranked, path_for_pw: str, timeout_ms: float) -> bool:
    """
    set_input_files(路径) 后 files 仍为空时，用系统文件选择器注入路径。
    微信页面上 input 常为隐藏：用 JS el.click() + expect_file_chooser，避免「Element is not visible」。
    """
    logger.info('  尝试 FileChooser 挂接（解隐藏 + JS click）')
    await _unhide_all_file_inputs(page)
    await page.wait_for_timeout(250)

    # 1) 每个 frame 里按索引 JS 点击对应 file input（最可靠）
    for fr, i, _, _, fr_url in ranked:
        try:
            async with page.expect_file_chooser(timeout=25000) as fc_info:
                await fr.evaluate(
                    """([idx]) => {
                        const a = [...document.querySelectorAll('input[type=file]')];
                        const el = a[idx];
                        if (el) { el.click(); }
                    }""",
                    [i],
                )
            chooser = await fc_info.value
            await chooser.set_files(path_for_pw, timeout=timeout_ms)
            await page.wait_for_timeout(800)
            if await _any_input_has_files(page):
                logger.info(
                    f'  Files attached via FileChooser (JS click input[{i}] '
                    f'frame={fr_url[:70]})'
                )
                return True
        except Exception as e:
            logger.info(f'  FileChooser JS input[{i}]: {e}')

    # 2) 点常见上传文案（部分布局无原生 chooser，仅作补充）
    hint = re.compile(
        r'上传视频|选择视频|点击上传|拖拽|从相册|本地上传|添加视频|选取文件|选择文件'
    )
    try:
        async with page.expect_file_chooser(timeout=12000) as fc_info:
            await page.get_by_text(hint).first.click(timeout=6000, force=True)
        chooser = await fc_info.value
        await chooser.set_files(path_for_pw, timeout=timeout_ms)
        await page.wait_for_timeout(800)
        if await _any_input_has_files(page):
            logger.info('  Files attached via FileChooser (上传区文案)')
            return True
    except Exception as e:
        logger.info(f'  FileChooser 上传区: {e}')

    return False


def _defer_upload_temp_cleanup(page, cleanup_fn, remux_cleanup_fn):
    """
    挂接成功后 Chromium 仍要从磁盘分片读取整个视频；若在 _attach_video_to_page 的
    finally 里立刻 remux_cleanup()，磁盘文件被删，前端 FileReader 会报
    NotFoundError: A requested file or directory could not be found（你日志里已出现）。
    把 cleanup / remux_cleanup 挂到 page 上，由 process_video 在上传等待结束后再执行。
    """
    _flush_upload_temp_cleanup(page)
    page._wx_upload_temp_cleanups = (cleanup_fn, remux_cleanup_fn)
    logger.info(
        '  挂接成功：remux/ascii 临时文件延后到「上传等待」结束后再删 '
        '（否则浏览器读文件会 NotFoundError）'
    )


def _flush_upload_temp_cleanup(page):
    """执行并清空 page._wx_upload_temp_cleanups（ascii 副本 + remux 产物）。"""
    pair = getattr(page, '_wx_upload_temp_cleanups', None)
    if not pair:
        return
    page._wx_upload_temp_cleanups = None
    cleanup_fn, remux_cleanup_fn = pair
    try:
        if cleanup_fn:
            cleanup_fn()
    except Exception as e:
        logger.info(f'  延后清理 ascii 副本: {e}')
    try:
        if remux_cleanup_fn:
            remux_cleanup_fn()
    except Exception as e:
        logger.info(f'  延后清理 remux 文件: {e}')


async def _attach_video_to_page(page, video_path):
    """Attach video to the correct input[type=file]; 含 iframe、无头兼容与 FilePayload 回退。"""
    remuxed_path, remux_cleanup = _maybe_remux_to_mp4(video_path)
    src_for_attach = remuxed_path or video_path
    path_for_pw, cleanup = _ensure_ascii_upload_path(src_for_attach)
    # 成功挂接后改为 noop，避免 finally 立刻删盘；失败路径仍用原 cleanup
    arms = {'cleanup': cleanup, 'remux_cleanup': remux_cleanup}

    def _mark_attached_defer_disk_cleanup():
        _defer_upload_temp_cleanup(page, arms['cleanup'], arms['remux_cleanup'])
        arms['cleanup'] = lambda: None
        arms['remux_cleanup'] = None

    front_rejected_once = False
    try:
        await _prime_upload_zone(page)
        await _install_upload_diagnostics(page)
        # 先快速试一次：micro iframe 里如果已经有 input 就用它（更"正"），
        # 但绝不死等 —— 主 frame 的 input 实测也能挂上并触发上传。
        await _wait_for_micro_iframe(page, timeout_ms=1500)
        ranked = await _collect_video_file_inputs(page)
        if not ranked:
            raise Exception('No input[type=file] in main frame or iframes')
        logger.info(
            f'  挂接前 inputs 总数：{len(ranked)}（按 frame 优先级排序，micro/content 优先）'
        )
        for j, (_fr, _i, _pr, _acc, _u) in enumerate(ranked):
            logger.info(
                f'    rank#{j} pr={_pr} input[{_i}] accept="{_acc}" frame={_u[:80]}'
            )

        _pto = _set_files_timeout_ms(path_for_pw)
        logger.info(f'  set_input_files 超时设为 {int(_pto / 1000)}s（按文件大小）')
        try:
            page._wx_upload_source_bytes = os.path.getsize(path_for_pw)
        except OSError:
            page._wx_upload_source_bytes = 0
        await _unhide_all_file_inputs(page)
        await _install_file_clear_protection(page)
        await page.wait_for_timeout(200)
        for fr, i, prio, acc, fr_url in ranked:
            try:
                inp = fr.locator('input[type=file]').nth(i)
                await inp.wait_for(state='attached', timeout=3000)
                logger.info(
                    '  正在 set_input_files（大文件可能需数十秒，此阶段日志较少属正常）…'
                )
                # 在 set_input_files 前做一次 UI baseline，便于判 delta
                pre_signal = await _snapshot_upload_signals(page)
                pre_diag = await _read_upload_diagnostics(page)
                await inp.set_input_files(path_for_pw, timeout=_pto)
                # 立刻读一次详情：判断是「Playwright 没挂上」还是「挂上后被前端清空」
                await page.wait_for_timeout(150)
                imm = await _inspect_input_files(inp)
                stats = await _read_clear_protection_stats(page)
                pw_imm_len = (imm or {}).get('length') or 0
                if imm:
                    logger.info(
                        f'  set_input_files 完成（即时检查）length={imm.get("length")} '
                        f'items={imm.get("items")} 清空保护：blocks={stats["blocks"]}'
                    )
                    if pw_imm_len == 0:
                        front_rejected_once = True

                # 即便 length=0，也再观察 ~10 秒看页面有没有出现上传进度/缩略图，
                # 如果出现说明 set_input_files 其实成功（React 已 onChange 启动上传）
                if pw_imm_len == 0:
                    ok_pw_ui = False
                    pw_evi = ''
                    ok_pw_diag = False
                    diag_evi = ''
                    after_pw = pre_signal
                    for _t in range(20):
                        await page.wait_for_timeout(500)
                        after_pw = await _snapshot_upload_signals(page)
                        after_diag = await _read_upload_diagnostics(page)
                        ok_pw_ui, pw_evi = _signals_imply_upload(pre_signal, after_pw)
                        ok_pw_diag, diag_evi = _diag_implies_real_upload(pre_diag, after_diag)
                        if ok_pw_ui or ok_pw_diag:
                            break
                    if ok_pw_ui or ok_pw_diag:
                        evidence = diag_evi if ok_pw_diag else pw_evi
                        logger.info(
                            f'  set_input_files 实际成功（input.files 已被 React reset，'
                            f'但页面/网络信号显示开始上传：{evidence}）'
                        )
                        _mark_attached_defer_disk_cleanup()
                        return

                # length=0 说明 Playwright 实际没挂上（或挂上瞬间被清空）
                # → 直接走 CDP，不再傻等 20s
                if pw_imm_len == 0:
                    logger.info(
                        f'  Playwright 即时 length=0，直接尝试 CDP DOM.setFileInputFiles '
                        f'(input[{i}] …)'
                    )
                    # 挂接前先做一次 UI 信号 baseline，以便判 delta
                    before = await _snapshot_upload_signals(page)
                    before_diag = await _read_upload_diagnostics(page)
                    if await _attach_via_cdp_dom_set_file_input_files(
                        page, fr, i, path_for_pw
                    ):
                        # 关键：React 受控 input 拿到 onChange 后通常会立刻把 input.value=''，
                        # 所以 input.files=0 并不代表失败 —— 改用「页面 UI 信号 delta」判定。
                        peak = 0
                        last_len = 0
                        ok_by_ui = False
                        ui_evidence = ''
                        ok_by_diag = False
                        diag_evidence = ''
                        after = before
                        # 最多 ~30 秒等页面响应（前端可能要做秒传校验/分片准备）
                        for k in range(60):
                            try:
                                last_len = await inp.evaluate(
                                    'el => (el.files && el.files.length) || 0'
                                )
                            except Exception:
                                last_len = 0
                            if last_len > peak:
                                peak = last_len
                            # 每 ~1s 抽查一次 UI 信号
                            if k > 0 and k % 2 == 0:
                                after = await _snapshot_upload_signals(page)
                                after_diag = await _read_upload_diagnostics(page)
                                ok_by_ui, ui_evidence = _signals_imply_upload(before, after)
                                ok_by_diag, diag_evidence = _diag_implies_real_upload(
                                    before_diag, after_diag
                                )
                                if ok_by_ui or ok_by_diag:
                                    break
                            await page.wait_for_timeout(500)
                        stats2 = await _read_clear_protection_stats(page)
                        logger.info(
                            f'  CDP 后观察：peak_files={peak}, last_files={last_len}, '
                            f'页面信号 before={before} after={after}, '
                            f'判定开始上传={ok_by_ui or ok_by_diag} '
                            f'({diag_evidence or ui_evidence}), '
                            f'清空保护 blocks={stats2["blocks"]}'
                        )
                        await _dump_all_file_inputs(page)
                        if last_len > 0 or ok_by_ui or ok_by_diag:
                            reason = (
                                f'input.files={last_len}' if last_len > 0
                                else f'网络/UI 信号：{diag_evidence or ui_evidence}'
                            )
                            logger.info(
                                f'  Files attached via CDP（{reason}） '
                                f'(frame "{fr_url[:80]}...", input[{i}])'
                            )
                            _mark_attached_defer_disk_cleanup()
                            return
                        if peak > 0 and last_len == 0:
                            front_rejected_once = True
                            logger.info(
                                '  CDP 已成功把文件交给浏览器（peak>0），但前端瞬间清空了 '
                                'files 且页面无上传迹象 —— 视为前端拒绝'
                            )
                else:
                    # Playwright 即时已经挂上，正常等浏览器确认稳定
                    n_files = await _poll_input_file_count(
                        inp,
                        page,
                        '等待浏览器确认 input.files（是否接受挂接的文件）',
                    )
                    if n_files > 0:
                        logger.info(
                            f'  Files attached: {n_files} (frame "{fr_url[:80]}...", '
                            f'input[{i}], priority={prio}, accept="{acc or "(none)"}")'
                        )
                        _mark_attached_defer_disk_cleanup()
                        return
                    logger.info(
                        f'  Playwright 后 files 又变 0，转 CDP DOM.setFileInputFiles '
                        f'(input[{i}] …)'
                    )
                    if await _attach_via_cdp_dom_set_file_input_files(
                        page, fr, i, path_for_pw
                    ):
                        n_files = await _poll_input_file_count(
                            inp, page, 'CDP 挂接后等待 input.files'
                        )
                        if n_files > 0:
                            logger.info(
                                f'  Files attached via CDP: {n_files} '
                                f'(frame "{fr_url[:80]}...", input[{i}], '
                                f'accept="{acc or "(none)"}")'
                            )
                            _mark_attached_defer_disk_cleanup()
                            return
            except Exception as ex:
                logger.info(f'  input[{i}] set_input_files failed ({fr_url[:60]}...): {ex}')

        _try_fc = os.environ.get('TRY_FILECHOOSER', '').strip().lower() in (
            '1',
            'true',
            'yes',
            'on',
        )
        if _try_fc:
            if await _attach_via_file_chooser(page, ranked, path_for_pw, _pto):
                _mark_attached_defer_disk_cleanup()
                return
        else:
            logger.info(
                '  跳过 FileChooser（默认关闭，省约 30s；'
                '若路径挂接后 files 仍为空可设 TRY_FILECHOOSER=1 再试）'
            )

        # 仅 ≤50MiB：Playwright 允许内存 FilePayload（再大必须用路径 / FileChooser）
        fsz = os.path.getsize(path_for_pw)
        if fsz <= _PLAYWRIGHT_FILE_PAYLOAD_MAX:
            logger.info(
                f'  尝试内存 FilePayload（{fsz / 1024 / 1024:.1f} MiB，Playwright 上限 50MiB）'
            )
            with open(path_for_pw, 'rb') as _bf:
                buf = _bf.read()
            payloads = [
                ('video.mov', 'video/quicktime'),
                ('video.mp4', 'video/mp4'),
                ('video.mov', 'video/mp4'),
            ]
            for fr, i, prio, acc, fr_url in ranked:
                inp = fr.locator('input[type=file]').nth(i)
                for fname, mime in payloads:
                    try:
                        await inp.wait_for(state='attached', timeout=3000)
                        if await _try_filepayload_attach(inp, page, buf, fname, mime):
                            logger.info(
                                f'  Files attached via FilePayload ({fname}, {mime}) '
                                f'input[{i}] frame={fr_url[:70]}'
                            )
                            _mark_attached_defer_disk_cleanup()
                            return
                    except Exception as ex:
                        logger.info(f'  FilePayload {fname}/{mime} failed: {ex}')
        elif fsz > _PLAYWRIGHT_FILE_PAYLOAD_MAX:
            logger.info(
                f'  文件 {fsz / 1024 / 1024:.0f} MiB > 50MiB，跳过内存 FilePayload（Playwright 限制）'
            )

        await _save_upload_debug(page, 'no_files_attached')

        if front_rejected_once:
            raise Exception(
                '视频被发表页 onChange 立即清空（Playwright 已挂上，但页面前端拒绝）— '
                '通常是「视频编码浏览器无法解码」（macOS .mov 多为 HEVC/ProRes）。'
                '强烈建议用 FFmpeg 重新编码为 H.264/AAC MP4：'
                'ffmpeg -i in.mov -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart out.mp4'
            )

        raise Exception(
            'No files attached after trying all file inputs — '
            '已尝试 Playwright set_input_files + CDP DOM.setFileInputFiles +（可选）FilePayload/FileChooser。'
            '若仍失败：换 H.264 MP4、纯英文路径、或 SKIP_CDP_SET_FILES=1 排除 CDP 干扰后再试。'
            + (
                ' 大文件可设 TRY_FILECHOOSER=1 再试 FileChooser。'
                if fsz > _PLAYWRIGHT_FILE_PAYLOAD_MAX
                else ''
            )
        )
    finally:
        try:
            if arms['cleanup']:
                arms['cleanup']()
        finally:
            rc = arms['remux_cleanup']
            if rc:
                try:
                    rc()
                except Exception:
                    pass


# ── Main upload logic ──
async def process_video(browser_context, record):
    """Process a single video record: upload, fill metadata, and publish."""
    pages = [p for p in browser_context.pages if not p.is_closed()]
    page = pages[0] if pages else None
    if not page:
        logger.warn('  No live page, creating new page...')
        page = await browser_context.new_page()

    # 浏览器页面的 console / pageerror 不再转发到日志：
    # 视频号发表页常年伴随大量 "ERR_FILE_NOT_FOUND"、"Mixed Content" 等噪音，与上传是否成功无关，
    # 反而把"发布成功 / 上传进度"等关键日志淹没。需要排错时改用 VERBOSE_LOG=1 + DevTools。

    result = {
        'video_path': record.get('video_path', ''),
        'title': record.get('title', ''),
        'status': 'unknown',
        'error': '',
        '_errorType': 'fatal',
        '_loginExpired': False,
    }

    try:
        # 重试/上一条若遗留了「延后删盘」，先清掉，避免占满 uploads/ 或干扰本轮
        _flush_upload_temp_cleanup(page)
        logger.info(f'\n=== 发表视频：{record.get("title", "")} ===')
        # 先导航到平台首页，再通过导航进入发表页面
        await page.goto('https://channels.weixin.qq.com/platform',
                        wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(5000)
        login_state = await detect_login_state(page)
        if not login_state.get('logged_in'):
            result['status'] = 'failed'
            result['error'] = '当前未登录，请先扫码登录'
            result['_loginExpired'] = True
            result['_errorType'] = 'login-expired'
            return result

        # 先直达发表页；部分版本会落到 /micro/content/post/create
        if '/post/create' not in page.url:
            logger.info('  Trying direct URL /platform/post/create...')
            try:
                await page.goto('https://channels.weixin.qq.com/platform/post/create',
                                wait_until='domcontentloaded', timeout=20000)
                # 给页面 JS 运行一点时间，防止立即重定向
                await page.wait_for_timeout(3000)
                
                # 如果被重定向回首页，再试一次
                if '/post/create' not in page.url:
                    logger.info('  Redirected, retrying direct URL...')
                    await page.goto('https://channels.weixin.qq.com/platform/post/create',
                                    wait_until='domcontentloaded', timeout=20000)
                    await page.wait_for_timeout(3000)
                if '/post/create' not in page.url:
                    logger.info('  Falling back to direct URL /micro/content/post/create...')
                    await page.goto('https://channels.weixin.qq.com/micro/content/post/create',
                                    wait_until='domcontentloaded', timeout=20000)
                    await page.wait_for_timeout(2500)
            except Exception as e:
                logger.info(f'  Direct URL /platform/post/create failed: {e}')

        # 如果直达后仍未出现上传控件，再尝试通过侧边菜单进入发表页
        if '/post/create' not in page.url or not await _collect_video_file_inputs(page):
            cur_url = (page.url or '').lower()
            # 先判定是否处于“可见导航容器”页面，再决定是否走菜单点击
            nav_containers = [
                page.locator('.menu-list, .menu-panel, .left-menu, aside[role="navigation"]').first,
                page.locator('[class*="menu"]').filter(has_text=re.compile(r'内容管理|发表视频|视频管理')).first,
            ]
            nav_visible = False
            for nc in nav_containers:
                try:
                    if await nc.count() > 0 and await nc.is_visible():
                        nav_visible = True
                        break
                except Exception:
                    continue

            if '/micro/content/post/create' in cur_url and not nav_visible:
                logger.info('  Already in micro create page and no visible nav container; skip menu navigation')
            else:
                logger.info('  Looking for upload entry via navigation...')
                # 给左侧导航一点渲染时间，避免过早判定 miss
                await page.wait_for_timeout(800)
                nav_steps = [
                    {'action': 'click', 'selector': page.get_by_text('内容管理', exact=True),
                     'desc': '内容管理'},
                    {'action': 'click', 'selector': page.get_by_text(
                        re.compile(r'内容管理|发表视频|视频管理')),
                     'desc': '内容/发表/视频管理 (模糊)'},
                    {'action': 'click', 'selector': page.get_by_text('发表视频', exact=True),
                     'desc': '发表视频'},
                    {'action': 'click', 'selector': page.get_by_role(
                        'button', name=re.compile(r'发布视频|发表视频|上传视频|创作')),
                     'desc': '发布/发表/上传/创作按钮'},
                ]

                for step in nav_steps:
                    try:
                        # 如果当前已经在发表页了，就不再点菜单
                        if '/post/create' in page.url and await _collect_video_file_inputs(page):
                            break
                        el = step['selector'].first
                        found_visible = False
                        try:
                            await el.wait_for(state='visible', timeout=1200)
                            found_visible = True
                        except Exception:
                            found_visible = False
                        if found_visible:
                            await el.click(timeout=3000)
                            logger.info(f'  Clicked: {step["desc"]}')
                            await page.wait_for_timeout(3000)
                        else:
                            logger.info(f'  Nav miss: {step["desc"]}')
                    except Exception:
                        logger.info(f'  Nav error: {step["desc"]}')

        # 触发上传区域
        await _prime_upload_zone(page)
        # 等待 iframe
        await _wait_for_micro_iframe(page, timeout_ms=15000)

        # 增强：等待上传区域出现，增加重试点击逻辑
        t_deadline = time.time() + 30 # 延长到 30s
        found_input = False
        last_wait_log_sec = -1
        while time.time() < t_deadline:
            if await _collect_video_file_inputs(page):
                found_input = True
                break
            # 每隔 3 秒尝试重新点击一下上传区，或者刷新页面
            elapsed = 30 - (t_deadline - time.time())
            cur_sec = int(elapsed)
            if elapsed > 5 and cur_sec % 4 == 0 and cur_sec != last_wait_log_sec:
                last_wait_log_sec = cur_sec
                logger.info(f'  Still waiting for input ({int(elapsed)}s)... re-priming zone')
                await _prime_upload_zone(page)
            
            # 如果等了 10 秒还没出来，尝试重新进入发表页
            if int(elapsed) == 10:
                logger.info('  Waiting too long, trying direct URL again...')
                try:
                    await page.goto('https://channels.weixin.qq.com/platform/post/create',
                                    wait_until='domcontentloaded', timeout=15000)
                    await _wait_for_micro_iframe(page, timeout_ms=5000)
                except Exception:
                    pass
                    
            await page.wait_for_timeout(400)
        
        if not found_input:
            try:
                frame_urls = [((fr.url or '')[:120]) for fr in page.frames]
                logger.info(f'  当前页面 URL: {page.url}')
                logger.info(f'  当前 frame 列表: {frame_urls}')
            except Exception:
                pass
            if not os.path.exists(SCREENSHOTS_DIR):
                os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            ts = int(time.time() * 1000)
            sfx = _safe_filename(record.get('title', '') or 'unknown', 30)
            await page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, f'debug_{ts}_{sfx}.png'),
                full_page=True
            )
            with open(os.path.join(SCREENSHOTS_DIR, f'debug_{ts}_{sfx}.html'),
                      'w', encoding='utf-8') as f:
                f.write(await page.content())
            logger.info(f'  Debug: screenshots/debug_{ts}_{sfx}.png + .html (url: {page.url})')
            raise Exception(
                'input[type=file] not found after 30s — '
                'page may have changed or iframe failed to load. Screenshot saved.'
            )

        logger.info(f'  Upload: {record.get("video_path", "")}')
        video_path = record.get('video_path', '')
        if not os.path.exists(video_path):
            raise Exception(f'File not found: {video_path}')

        ranked_hint = await _collect_video_file_inputs(page)
        logger.info(f'  File inputs (all frames): {len(ranked_hint)}')
        for j, row in enumerate(ranked_hint[:6]):
            _, idx, _, acc, fr_url = row
            logger.info(f'    [{j}] input[{idx}] accept="{acc or "(none)"}" frame={fr_url[:90]}')

        try:
            await _attach_video_to_page(page, video_path)
            upload_result = await wait_for_upload_with_progress(
                page,
                record.get('_abortSignal'),
                video_path=video_path,
                record=record,
            )
        finally:
            # 必须在「上传等待」结束后再删 remux/ascii 临时文件，否则浏览器读盘会 NotFoundError
            _flush_upload_temp_cleanup(page)

        if upload_result == 'aborted':
            result['status'] = 'failed'
            result['error'] = '用户已手动停止'
            return result
        if upload_result in ('timeout', 'not_started', 'stuck_progress'):
            result['status'] = 'failed'
            result['_errorType'] = 'upload-failed'
            if upload_result == 'timeout':
                result['error'] = (
                    f'上传等待超时（已等满 UPLOAD_WAIT_MAX_SEC 相关上限）；'
                    f'大文件请尽量用有线网络、H.264 MP4、或继续调大 UPLOAD_WAIT_MAX_SEC。'
                )
            elif upload_result == 'not_started':
                result['error'] = '上传未开始，请重试'
            else:
                result['error'] = (
                    '上传进度长时间停在同一百分比（界面会似「卡死」）；'
                    '多为网络不稳定、代理/VPN、文件过大或微信侧限流。'
                    '可尝试：换网络、关闭代理、压缩/切片视频、在页面点「取消上传」后重试；'
                    '或调大 UPLOAD_STUCK_SAME_PCT_SEC（默认 900）再观察。'
                )
            return result
        await page.wait_for_timeout(5000)
        login_state = await detect_login_state(page)
        if not login_state.get('logged_in'):
            result['status'] = 'failed'
            result['error'] = '上传过程中登录已失效，请重新扫码登录'
            result['_loginExpired'] = True
            result['_errorType'] = 'login-expired'
            return result

        cover_path = record.get('cover_path', '').strip() if record.get('cover_path') else ''
        if cover_path:
            await set_cover(page, cover_path)

        await hide_location(page)
        login_state = await detect_login_state(page)
        if not login_state.get('logged_in'):
            result['status'] = 'failed'
            result['error'] = '登录已失效，请重新扫码登录'
            result['_loginExpired'] = True
            result['_errorType'] = 'login-expired'
            return result

        title_val = record.get('title', '').strip() if record.get('title') else ''
        if title_val:
            logger.info(f'  Title: {title_val}')
            await page.get_by_role('textbox',
                                   name=re.compile(r'概括视频主要内容')).fill(title_val)

        desc = record.get('description', '')
        if desc:
            logger.info('  Description')
            editor = page.locator('.input-editor')
            await editor.click()
            await editor.evaluate('el => { el.textContent = ""; }')
            await page.keyboard.type(desc)

        drama_name = record.get('short_drama_name', '')
        if drama_name:
            await select_short_drama(page, drama_name)

        # 定时发表（用视频号官方功能）：严格在视频上传完成后再执行，避免干扰上传过程。
        publish_time_raw = (record.get('publish_time') or '').strip()
        scheduled_ok = False
        pt_dt = None
        if publish_time_raw:
            try:
                pt_dt = datetime.fromisoformat(publish_time_raw)
            except Exception:
                pt_dt = None
            if pt_dt is not None:
                allowed, why = publish_timer_allowed(record, pt_dt)
                if allowed:
                    scheduled_ok = await set_schedule_publish(page, pt_dt)
                    if not scheduled_ok:
                        raise Exception('定时设置失败：尚未确认页面已进入定时状态，已阻止直接发布')
                else:
                    iv = record_schedule_interval_minutes(record)
                    if why == 'past':
                        logger.warn(
                            f'  定时时间 {publish_time_raw} 已不晚于当前时间，按立即发表处理（不定时）'
                        )
                    else:
                        logger.warn(
                            f'  定时时间 {publish_time_raw} 距现在在 {iv + 5} 分钟内（间隔 {iv} 分钟 + 5 分钟缓冲），'
                            f'按立即发表处理（不定时）'
                        )

        logger.info('  Clicking 发表...')
        await page.get_by_role('button', name='发表').click()

        if await verify_publish(page):
            result['status'] = 'published'
            if scheduled_ok:
                logger.info(
                    f'  发布成功：{record.get("title", "")}'
                    f'（已设定时，{publish_time_raw} 由微信官方自动发布）'
                )
            else:
                logger.info(f'  发布成功：{record.get("title", "")}')
        else:
            if not os.path.exists(SCREENSHOTS_DIR):
                os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
            ss = f'{int(time.time() * 1000)}_{_safe_filename(record.get("title", "") or "unknown", 50)}.png'
            await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, ss), full_page=False)
            logger.info(f'  Screenshot: {ss}')
            result['status'] = 'uncertain'
            try:
                body_text = await page.locator('body').text_content()
                result['error'] = (body_text or '')[:200]
            except Exception:
                result['error'] = ''

    except Exception as e:
        result['status'] = 'failed'
        result['error'] = str(e)
        result['_errorType'] = classify_error(str(e))
        logger.error(f'  FAILED: {e}')

    return result


async def wait_until(target_time):
    """已废弃：定时发表改为使用视频号官方功能（在 process_video 里勾选「定时」 + 填发表时间）。
    保留函数仅为向后兼容；不要再在新代码里调用它。
    """
    now = datetime.now()
    diff = (target_time - now).total_seconds()
    if diff > 0:
        logger.warn(
            f'  [deprecated] wait_until({target_time}) was called '
            f'({round(diff / 60)} min) — 定时发表已迁移到官方功能，无需阻塞等待'
        )
        await asyncio.sleep(diff)


def handle_login_expired():
    """登录态过期：写日志 + 桌面通知（best-effort）。
    Web 模式：在 UI 对应账号点「登录」重新扫码即可，不需要跑 CLI。"""
    logger.error('登录已过期，请在 UI 上点对应账号的「登录」重新扫码')
    notify_user('视频号上传 - 登录过期', '登录态已过期，请重新扫码登录。')


# ── Batch process (used by both CLI and server) ──
async def batch_upload(browser_context, records, options=None):
    """Process multiple video records sequentially, with retry and resume support."""
    if options is None:
        options = {}
    results_path = options.get('resultsPath', RESULTS_PATH)
    resume = options.get('resume', False)
    existing_results = options.get('results', [])
    abort_signal = options.get('abortSignal')
    on_progress = options.get('onProgress')
    on_login_expired = options.get('onLoginExpired')

    results = list(existing_results)
    published_set = load_published_titles(results_path, resume)
    login_expired_flag = False

    # Close extra tabs, ensure at least one LIVE page exists
    pages = browser_context.pages
    for i in range(len(pages) - 1, 0, -1):
        await pages[i].close()
    # 过滤掉已关闭的页面
    live = [p for p in browser_context.pages if not p.is_closed()]
    if not live:
        await browser_context.new_page()

    total = len(records)
    for i, record in enumerate(records):
        if _check_abort(abort_signal):
            logger.warn('Upload aborted by user')
            break

        record['_abortSignal'] = abort_signal
        if record.get('_skip'):
            results.append({
                'video_path': record.get('video_path', ''),
                'title': record.get('title', ''),
                'status': 'skipped',
                'error': record.get('_skipReason', ''),
            })
            if on_progress:
                on_progress({'current': i + 1, 'total': total,
                             'status': 'skipped', 'title': record.get('title', '')})
            continue

        if record.get('title', '') in published_set:
            results.append({
                'video_path': record.get('video_path', ''),
                'title': record.get('title', ''),
                'status': 'published',
                'error': '',
            })
            if on_progress:
                on_progress({'current': i + 1, 'total': total,
                             'status': 'published', 'title': record.get('title', '')})
            continue

        if login_expired_flag:
            results.append({
                'video_path': record.get('video_path', ''),
                'title': record.get('title', ''),
                'status': 'failed',
                'error': '登录已失效，请重新扫码登录',
            })
            continue

        # 注意：「定时发表」改为使用视频号官方功能（在 process_video 里勾选「定时」 +
        # 填发表时间），上传过程不再阻塞等待目标时间。

        result = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                logger.info(f'  Retry {attempt}/{MAX_RETRIES}')
                await asyncio.sleep(3)
            result = await process_video(browser_context, record)
            if result['status'] == 'published' or result.get('_errorType') in (
                    'login-expired', 'title-error', 'upload-failed'):
                break

        results.append(result)
        if result.get('_loginExpired'):
            login_expired_flag = True
            handle_login_expired()
            if on_login_expired:
                on_login_expired(record)
        write_results(results, results_path)
        if on_progress:
            on_progress({'current': i + 1, 'total': total,
                         'status': result['status'], 'title': record.get('title', '')})

        # 排队：上一条已成功发表后，固定等待 5 秒再开始下一条（最后一条不等待）
        if i + 1 < total and result.get('status') == 'published':
            wait_sec = 5
            logger.info(f'  批量排队：上一条已发表，固定等待 {wait_sec} 秒后开始下一条…')
            if not await _async_sleep_interruptible(wait_sec, abort_signal):
                logger.warn('  批量排队等待中被中止，不再处理后续条目')
                break

    return results


# ── CLI entry ──
async def main():
    """CLI entry point for batch upload."""
    args = sys.argv[1:]
    is_setup = '--setup' in args
    csv_idx = args.index('--csv') if '--csv' in args else -1
    if csv_idx >= 0:
        csv_path = os.path.abspath(args[csv_idx + 1])
    else:
        csv_path = os.path.join(_BASE_DIR, 'batch-config.csv')
    resume = '--resume' in args

    if os.path.exists(LOG_PATH):
        os.unlink(LOG_PATH)
    unlock_profile(PROFILE_DIR)

    _headless = upload_headless_from_env()
    logger.info(f'Opening browser... (headless={_headless})')
    browser_context = await init_browser(PROFILE_DIR, headless=_headless)

    # Register SIGTERM handler (best-effort on Windows)
    try:
        signal.signal(signal.SIGTERM, lambda sig, frame: os._exit(0))
    except (ValueError, AttributeError):
        pass

    try:
        if is_setup:
            await login_flow(browser_context)
            await browser_context.close()
            return

        records = None
        try:
            records = load_csv(csv_path)
            logger.info(f'Loaded {len(records)} records')
        except Exception as e:
            logger.error(f'CSV: {e}')
            await browser_context.close()
            sys.exit(1)

        records = preflight_records(records)
        valid_count = len([r for r in records if not r.get('_skip')])
        if valid_count == 0:
            logger.warn('No valid records')
            await browser_context.close()
            return
        logger.info(f'Preflight: {valid_count} valid, {len(records) - valid_count} skipped')

        results = await batch_upload(browser_context, records, {
            'resume': resume,
            'resultsPath': RESULTS_PATH,
        })

        published_count = len([r for r in results if r.get('status') == 'published'])
        logger.info(f'\nDone. {published_count}/{len(results)} published')
        logger.info('Browser left open. Close when done.')
        write_results(results, RESULTS_PATH)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info('Shutting down...')
        try:
            await browser_context.close()
        except Exception:
            pass
        sys.exit(0)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
