#!/usr/bin/env python3
"""PyInstaller build script - supports Windows and macOS."""
import glob
import json
import os
import shutil
import subprocess
import sys


APP_NAME = '视频号批量上传'


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
        hidden += ['--hidden-import', 'webview.platforms.winforms']
    elif platform_name == 'macos':
        hidden += ['--hidden-import', 'webview.platforms.cocoa']
    return hidden


def build_pyinstaller_cmd(base, platform_name):
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--onedir',
        '--windowed',
        '--name', APP_NAME,
        '--add-data', f'public{os.pathsep}public',
        '--add-data', f'version.json{os.pathsep}.',
        '--add-data', f'accounts.json{os.pathsep}.',
        '--add-data', f'update.json{os.pathsep}.',
        '--collect-submodules', 'flask_socketio',
        '--collect-submodules', 'engineio.async_drivers.threading',
        '--copy-metadata', 'playwright',
        '--clean',
        '--noconfirm',
    ]
    cmd += get_hidden_imports(platform_name)
    cmd += get_icon_args(base, platform_name)
    if platform_name == 'macos':
        cmd += ['--osx-bundle-identifier', 'com.qibiao.wechat-channels-uploader']
    cmd += ['run.py']
    return cmd


def copy_playwright_browsers(base, platform_name, chromium_ver):
    ms_pw = get_ms_playwright_dir()
    if not ms_pw or not chromium_ver:
        log('[WARN] Chromium not found, run: playwright install chromium')
        log('[WARN] Packaged app will need Playwright Chromium installed separately')
        return

    chromium_dir = os.path.join(ms_pw, f'chromium-{chromium_ver}')
    if platform_name == 'windows':
        dist_internal = os.path.join(base, 'dist', APP_NAME, '_internal')
    else:
        dist_internal = os.path.join(base, 'dist', f'{APP_NAME}.app', 'Contents', 'MacOS', '_internal')
    if not (os.path.isdir(chromium_dir) and os.path.isdir(dist_internal)):
        log('[WARN] Skip bundling Chromium because target runtime directory was not found')
        return

    pw_dest = os.path.join(dist_internal, 'ms-playwright')
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
        return os.path.join(base, 'dist', APP_NAME)
    return os.path.join(base, 'dist', f'{APP_NAME}.app')


def make_release_archive(base, platform_name, version, target_path):
    dist_dir = os.path.join(base, 'dist')
    archive_base = os.path.join(dist_dir, f'{APP_NAME}-{platform_name}-v{version}')
    archive_path = archive_base + '.zip'
    if os.path.exists(archive_path):
        os.remove(archive_path)
    root_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
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


def build():
    configure_console_output()
    base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)
    platform_name = detect_platform()
    version = read_version(base)
    log(f'[INFO] Building version: {version}')
    log(f'[INFO] Platform: {platform_name}')

    chromium_ver = find_chromium_version()
    if chromium_ver:
        log(f'[INFO] Chromium revision: {chromium_ver}')

    try:
        import PyInstaller  # noqa: F401
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

    archive_path = make_release_archive(base, platform_name, version, target)
    total = get_target_size(target)
    log(f'[SUCCESS] Build complete: {target}')
    log(f'[INFO] Version: {version}')
    log(f'[INFO] Archive: {archive_path}')
    log(f'[INFO] Total size: {total / (1024 * 1024 * 1024):.2f} GB')


if __name__ == '__main__':
    build()
