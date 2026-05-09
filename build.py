#!/usr/bin/env python3
"""PyInstaller build script - supports Windows and macOS."""
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.request



APP_NAME = 'sph-app'
DISPLAY_NAME = '视频号批量上传'
WEBVIEW2_BOOTSTRAPPER_FILENAME = 'MicrosoftEdgeWebview2Setup.exe'
WEBVIEW2_BOOTSTRAPPER_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'


def configure_console_output():
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, 'reconfigure'):
            continue
        try:
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        except Exception:
            pass


def log(message):
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        stream = sys.stdout
        data = (text + '\n').encode(getattr(stream, 'encoding', None) or 'utf-8', errors='backslashreplace')
        buffer = getattr(stream, 'buffer', None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            stream.write(data.decode('utf-8', errors='replace'))
            stream.flush()


def detect_platform():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    raise SystemExit(f'[ERROR] Unsupported platform: {sys.platform}')


def get_ms_playwright_dir():
    candidates = []
    env_dir = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip()
    if env_dir and env_dir != '0':
        candidates.append(env_dir)
    if sys.platform == 'win32':
        candidates.append(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright'))
    elif sys.platform == 'darwin':
        candidates.append(os.path.expanduser('~/Library/Caches/ms-playwright'))
    else:
        candidates.append(os.path.expanduser('~/.cache/ms-playwright'))
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ''


def get_playwright_browsers_path_for_install():
    env_dir = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '').strip()
    if env_dir and env_dir != '0':
        return env_dir
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('LOCALAPPDATA', ''), 'ms-playwright')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Caches/ms-playwright')
    return os.path.expanduser('~/.cache/ms-playwright')


def find_chromium_version():
    """Find the Playwright Chromium revision from browsers.json."""
    try:
        import playwright
        pw_dir = os.path.dirname(playwright.__file__)
        candidates = [
            os.path.join(pw_dir, 'driver', 'package', 'browsers.json'),
            os.path.join(pw_dir, 'driver', 'browsers.json'),
            os.path.join(pw_dir, 'browsers.json'),
        ]
        for browsers_json in candidates:
            if not os.path.exists(browsers_json):
                continue
            with open(browsers_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for browser in data.get('browsers', []):
                if browser.get('name') == 'chromium':
                    return str(browser['revision'])
    except Exception:
        pass

    ms_pw = get_ms_playwright_dir()
    if os.path.isdir(ms_pw):
        versions = glob.glob(os.path.join(ms_pw, 'chromium-*'))
        if versions:
            versions.sort(key=lambda p: int(os.path.basename(p).split('-')[1]))
            return os.path.basename(versions[-1]).split('-')[1]
    return None


def _has_playwright_browser(ms_pw_dir, browser_name, revision=None):
    if not ms_pw_dir or not os.path.isdir(ms_pw_dir):
        return False
    if revision:
        return os.path.isdir(os.path.join(ms_pw_dir, f'{browser_name}-{revision}'))
    try:
        for d in os.listdir(ms_pw_dir):
            if d.startswith(f'{browser_name}-') and os.path.isdir(os.path.join(ms_pw_dir, d)):
                return True
    except OSError:
        return False
    return False


def _run_playwright_install(args, env):
    cmd = [sys.executable, '-m', 'playwright', 'install'] + args
    log(f'[INFO] Running: {" ".join(cmd)}')
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        raise SystemExit('[ERROR] Python executable not found when running playwright install')
    except subprocess.TimeoutExpired:
        raise SystemExit(
            '[ERROR] playwright install 超时（30 分钟）。\n'
            '[HINT] 请检查网络后重试，或在构建机先手动执行: python -m playwright install chromium chromium-headless-shell'
        )
    if result.returncode == 0:
        return
    tail = ''
    if result.stderr:
        tail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ''
    elif result.stdout:
        tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
    raise SystemExit(
        '[ERROR] 自动安装 Playwright 浏览器失败，已中止打包。\n'
        f'[DETAIL] {" ".join(cmd)} -> exit {result.returncode}'
        + (f' | {tail}' if tail else '') +
        '\n[HINT] 可先手动执行: python -m playwright install chromium chromium-headless-shell'
    )


def ensure_playwright_browsers_ready():
    chromium_ver = find_chromium_version()
    install_path = get_playwright_browsers_path_for_install()
    env = dict(os.environ)
    env['PLAYWRIGHT_BROWSERS_PATH'] = install_path
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = install_path
    os.makedirs(install_path, exist_ok=True)

    ms_pw = get_ms_playwright_dir() or install_path
    has_chromium = _has_playwright_browser(ms_pw, 'chromium', chromium_ver)
    has_shell = _has_playwright_browser(ms_pw, 'chromium_headless_shell', chromium_ver)
    missing_targets = []
    if not has_chromium:
        missing_targets.append('chromium')
    if not has_shell:
        missing_targets.append('chromium-headless-shell')

    if missing_targets:
        log(f'[INFO] Playwright browsers missing before build: {", ".join(missing_targets)}')
        _run_playwright_install(missing_targets, env)
        chromium_ver = find_chromium_version()
        ms_pw = get_ms_playwright_dir() or install_path
        has_chromium = _has_playwright_browser(ms_pw, 'chromium', chromium_ver)
        has_shell = _has_playwright_browser(ms_pw, 'chromium_headless_shell', chromium_ver)

    if not has_chromium or not has_shell:
        raise SystemExit(
            '[ERROR] Playwright 浏览器未补齐，已中止打包。\n'
            f'[DETAIL] chromium={has_chromium}, headless_shell={has_shell}, path={ms_pw}\n'
            '[HINT] 先执行: python -m playwright install chromium chromium-headless-shell'
        )

    if chromium_ver:
        log(f'[INFO] Playwright Chromium revision ready: {chromium_ver}')
    else:
        log('[INFO] Playwright Chromium is ready')
    return chromium_ver


def clean(base):
    for d in ['dist', 'build']:
        dp = os.path.join(base, d)
        if os.path.exists(dp):
            try:
                shutil.rmtree(dp)
            except PermissionError:
                log(f'[WARN] Cannot remove {dp}, file in use. Trying to continue...')
    for f in glob.glob(os.path.join(base, '*.spec')):
        try:
            os.remove(f)
        except OSError:
            pass


def read_version(base):
    try:
        with open(os.path.join(base, 'version.json'), 'r', encoding='utf-8') as f:
            return json.load(f).get('version', '0.0.0')
    except Exception:
        return '0.0.0'


def get_icon_args(base, platform_name):
    if platform_name == 'windows':
        ico = os.path.join(base, 'app.ico')
        return ['--icon', ico] if os.path.isfile(ico) else []
    if platform_name == 'macos':
        icns = os.path.join(base, 'app.icns')
        return ['--icon', icns] if os.path.isfile(icns) else []
    return []


def get_hidden_imports(platform_name):
    hidden = [
        '--hidden-import', 'engineio.async_drivers.threading',
        '--hidden-import', 'playwright.async_api',
        '--hidden-import', 'webview',
    ]
    if platform_name == 'windows':
        hidden += [
            '--hidden-import', 'webview.platforms.winforms',
            '--hidden-import', 'webview.platforms.edgechromium',
            '--hidden-import', 'clr',
            '--hidden-import', 'pythonnet',
            '--hidden-import', 'clr_loader',
        ]
    elif platform_name == 'macos':
        hidden += [
            '--hidden-import', 'webview.platforms.cocoa',
            '--hidden-import', 'objc',
            '--hidden-import', 'AppKit',
            '--hidden-import', 'Cocoa',
            '--hidden-import', 'Foundation',
            '--hidden-import', 'WebKit',
        ]
    return hidden


def get_platform_pyinstaller_args(platform_name):
    args = []
    if platform_name == 'windows':
        args += [
            '--collect-submodules', 'pythonnet',
            '--collect-submodules', 'clr_loader',
            '--copy-metadata', 'pythonnet',
        ]
    return args


def get_build_cache_dir(base):
    return os.path.join(base, '.build-cache')


def find_local_webview2_bootstrapper(base):
    candidates = [
        os.path.join(base, WEBVIEW2_BOOTSTRAPPER_FILENAME),
        os.path.join(get_build_cache_dir(base), WEBVIEW2_BOOTSTRAPPER_FILENAME),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ''


def download_webview2_bootstrapper(base):
    cache_dir = get_build_cache_dir(base)
    os.makedirs(cache_dir, exist_ok=True)
    target_path = os.path.join(cache_dir, WEBVIEW2_BOOTSTRAPPER_FILENAME)
    log(f'[INFO] Downloading WebView2 bootstrapper: {WEBVIEW2_BOOTSTRAPPER_URL}')
    req = urllib.request.Request(
        WEBVIEW2_BOOTSTRAPPER_URL,
        headers={'User-Agent': 'Mozilla/5.0 sph-app build bootstrapper'}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    with open(target_path, 'wb') as f:
        f.write(data)
    log(f'[INFO] Cached WebView2 bootstrapper: {target_path}')
    return target_path


def ensure_webview2_bootstrapper(base, platform_name):
    if platform_name != 'windows':
        return ''
    existing = find_local_webview2_bootstrapper(base)
    if existing:
        log(f'[INFO] Using existing WebView2 bootstrapper: {existing}')
        return existing
    try:
        return download_webview2_bootstrapper(base)
    except Exception as e:
        raise SystemExit(
            '[ERROR] 无法准备 Windows WebView2 安装器，发布包将无法离线自动补齐桌面运行库。\n'
            f'[DETAIL] {e}\n'
            f'[HINT] 手动将 {WEBVIEW2_BOOTSTRAPPER_FILENAME} 放到项目根目录后重新执行 python build.py'
        )


def build_pyinstaller_cmd(base, platform_name):
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onedir',
        '--windowed',
        '--name', DISPLAY_NAME,
        '--add-data', f'public{os.pathsep}public',
        '--add-data', f'version.json{os.pathsep}.',
        '--add-data', f'accounts.json{os.pathsep}.',
        '--add-data', f'update.json{os.pathsep}.',
        '--add-data', f'app.ico{os.pathsep}.',
        '--collect-submodules', 'flask_socketio',
        '--collect-submodules', 'engineio.async_drivers.threading',
        '--collect-all', 'webview',
        '--copy-metadata', 'pywebview',
        '--copy-metadata', 'playwright',
        '--clean',
        '--noconfirm',
    ]
    cmd += get_hidden_imports(platform_name)
    cmd += get_platform_pyinstaller_args(platform_name)
    cmd += get_icon_args(base, platform_name)
    if platform_name == 'macos':
        cmd += ['--osx-bundle-identifier', 'com.qibiao.wechat-channels-uploader']
    cmd += ['run.py']
    return cmd


def ensure_windows_desktop_runtime():
    if sys.platform != 'win32':
        return
    missing = []
    required_modules = (
        ('webview', 'pywebview'),
        ('clr', 'pythonnet'),
        ('clr_loader', 'clr_loader'),
    )
    for import_name, package_name in required_modules:
        try:
            __import__(import_name)
        except Exception:
            missing.append(package_name)
    if missing:
        raise SystemExit(
            '[ERROR] Windows 原生桌面依赖缺失，当前构建出的应用将无法启动桌面窗口。\n'
            f'[MISSING] {", ".join(missing)}\n'
            '[HINT] 先执行: python -m pip install -r requirements.txt'
        )


def ensure_macos_desktop_runtime():
    if sys.platform != 'darwin':
        return
    missing = []
    for module_name in ('webview', 'objc', 'AppKit', 'Foundation', 'WebKit'):
        try:
            __import__(module_name)
        except Exception:
            missing.append(module_name)
    if missing:
        raise SystemExit(
            '[ERROR] macOS 原生桌面依赖缺失，当前构建出的 .app 将无法启动原生窗口。\n'
            f'[MISSING] {", ".join(missing)}\n'
            '[HINT] 先执行: python3 -m pip install -r requirements.txt'
        )


def copy_playwright_browsers(base, platform_name, chromium_ver):
    ms_pw = get_ms_playwright_dir()
    if not ms_pw or not chromium_ver:
        log('[WARN] Chromium not found, run: playwright install chromium')
        log('[WARN] Packaged app will need Playwright Chromium installed separately')
        return

    chromium_dir = os.path.join(ms_pw, f'chromium-{chromium_ver}')
    if platform_name == 'windows':
        dist_internal = os.path.join(base, 'dist', DISPLAY_NAME, '_internal')
        pw_dest = os.path.join(dist_internal, 'ms-playwright')
    else:
        # On macOS, place it in Resources for better visibility/standard compliance
        pw_dest = os.path.join(base, 'dist', f'{DISPLAY_NAME}.app', 'Contents', 'Resources', 'ms-playwright')
    
    os.makedirs(pw_dest, exist_ok=True)
    log(f'[INFO] Copying Chromium to {pw_dest}')

    dest_chromium = os.path.join(pw_dest, f'chromium-{chromium_ver}')
    if not os.path.exists(dest_chromium):
        shutil.copytree(chromium_dir, dest_chromium)

    headless_src = os.path.join(ms_pw, f'chromium_headless_shell-{chromium_ver}')
    if os.path.isdir(headless_src):
        dest_headless = os.path.join(pw_dest, f'chromium_headless_shell-{chromium_ver}')
        if not os.path.exists(dest_headless):
            shutil.copytree(headless_src, dest_headless)

    ffmpeg_dir = None
    for d in os.listdir(ms_pw):
        if d.startswith('ffmpeg-'):
            ffmpeg_dir = os.path.join(ms_pw, d)
            break
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        dest_ffmpeg = os.path.join(pw_dest, os.path.basename(ffmpeg_dir))
        if not os.path.exists(dest_ffmpeg):
            shutil.copytree(ffmpeg_dir, dest_ffmpeg)

    log('[INFO] Chromium bundled successfully')


def get_dist_target(base, platform_name):
    if platform_name == 'windows':
        return os.path.join(base, 'dist', DISPLAY_NAME)
    return os.path.join(base, 'dist', f'{DISPLAY_NAME}.app')


def bundle_webview2_bootstrapper(base, platform_name, target_path):
    if platform_name != 'windows':
        return
    bootstrapper_path = ensure_webview2_bootstrapper(base, platform_name)
    destination = os.path.join(target_path, WEBVIEW2_BOOTSTRAPPER_FILENAME)
    shutil.copy2(bootstrapper_path, destination)
    log(f'[INFO] Bundled WebView2 bootstrapper: {destination}')


def make_release_archive(base, platform_name, version, target_path):
    dist_dir = os.path.join(base, 'dist')
    archive_base = os.path.join(dist_dir, f'{APP_NAME}-{platform_name}-v{version}')
    archive_path = archive_base + '.zip'
    if os.path.exists(archive_path):
        os.remove(archive_path)
    
    root_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
    
    if platform_name == 'macos':
        # Use ditto on macOS to preserve symlinks and Unicode filenames inside .app bundles.
        log(f'[INFO] Creating zip archive using ditto to preserve bundle structure...')
        try:
            subprocess.run(
                ['ditto', '-c', '-k', '--keepParent', base_name, archive_path],
                cwd=root_dir,
                check=True,
            )
        except Exception as e:
            log(f'[ERROR] ditto zip failed: {e}. Falling back to shutil (symlinks or filenames might break).')
            shutil.make_archive(archive_base, 'zip', root_dir=root_dir, base_dir=base_name)
    else:
        shutil.make_archive(archive_base, 'zip', root_dir=root_dir, base_dir=base_name)
        
    return archive_path


def get_target_size(target_path):
    total = 0
    if os.path.isfile(target_path):
        return os.path.getsize(target_path)
    for root, _dirs, files in os.walk(target_path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


import hashlib

def calculate_sha256(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

import subprocess
from PIL import Image

def convert_logo_to_icons(base):
    """Convert logo.jpeg to app.ico and app.icns."""
    logo_path = os.path.join(base, 'logo.jpeg')
    if not os.path.exists(logo_path):
        log(f'[WARN] logo.jpeg not found at {logo_path}, skipping icon conversion')
        return

    log(f'[INFO] Converting logo.jpeg to icons...')
    try:
        img = Image.open(logo_path)
        
        # Save as ICO
        ico_path = os.path.join(base, 'app.ico')
        icon_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        img.save(ico_path, format='ICO', sizes=icon_sizes)
        log(f'[SUCCESS] Created {ico_path}')

        # Save as ICNS (macOS)
        if sys.platform == 'darwin':
            icns_path = os.path.join(base, 'app.icns')
            img.save(icns_path, format='ICNS')
            log(f'[SUCCESS] Created {icns_path}')
        else:
            # On Windows/Linux, we can't easily save as ICNS with PIL without extra libs, 
            # but we can at least ensure the ICO exists.
            pass
    except Exception as e:
        log(f'[ERROR] Icon conversion failed: {e}')

def build():
    configure_console_output()
    base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)
    
    # 转换图标
    try:
        convert_logo_to_icons(base)
    except Exception as e:
        log(f'[WARN] Icon conversion error: {e}')
        
    platform_name = detect_platform()
    version = read_version(base)
    log(f'[INFO] Building version: {version}')
    log(f'[INFO] Platform: {platform_name}')

    ensure_windows_desktop_runtime()
    ensure_macos_desktop_runtime()

    chromium_ver = ensure_playwright_browsers_ready()

    try:
        __import__('PyInstaller')
    except ModuleNotFoundError:
        raise SystemExit(
            '[ERROR] Missing dependency: PyInstaller\n'
            '[HINT] Run: python3 -m pip install -r requirements.txt'
        )

    clean(base)
    cmd = build_pyinstaller_cmd(base, platform_name)
    log('[INFO] Running PyInstaller...')
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit('[ERROR] PyInstaller failed')

    copy_playwright_browsers(base, platform_name, chromium_ver)

    for f in glob.glob(os.path.join(base, '*.spec')):
        try:
            os.remove(f)
        except OSError:
            pass

    target = get_dist_target(base, platform_name)
    if not os.path.exists(target):
        raise SystemExit(f'[ERROR] Build output not found: {target}')

    bundle_webview2_bootstrapper(base, platform_name, target)

    archive_path = make_release_archive(base, platform_name, version, target)
    total = get_target_size(target)
    sha256 = calculate_sha256(archive_path)
    log(f'[SUCCESS] Build complete: {target}')
    log(f'[INFO] Version: {version}')
    log(f'[INFO] Archive: {archive_path}')
    log(f'[INFO] SHA256: {sha256}')
    log(f'[INFO] Total size: {total / (1024 * 1024 * 1024):.2f} GB')

    # For GitHub Actions to capture
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        with open(github_output, 'a') as f:
            f.write(f'archive_sha256={sha256}\n')
            f.write(f'version={version}\n')
            f.write(f'platform={platform_name}\n')


if __name__ == '__main__':
    build()
