#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# run.py - 桌面启动器
# 配置 WebView2 环境后启动主程序
import sys

if sys.version_info < (3, 6):
    sys.stderr.write(
        'run.py requires Python 3.6+. Your "python" is an older version:\n  %s\n'
        'Use: python3 run.py\n' % sys.version.replace('\n', ' ')
    )
    raise SystemExit(1)

import os
import shutil

base_dir = os.path.dirname(os.path.abspath(__file__))
APP_DATA_FOLDER_NAME = '视频号批量上传'


def get_runtime_base_dir():
    if not getattr(sys, 'frozen', False):
        return base_dir
    if sys.platform == 'win32':
        support_root = (
            os.environ.get('LOCALAPPDATA')
            or os.environ.get('APPDATA')
            or os.path.expanduser('~')
        )
        support_dir = os.path.join(support_root, APP_DATA_FOLDER_NAME)
        os.makedirs(support_dir, exist_ok=True)
        return support_dir
    if sys.platform == 'darwin':
        support_dir = os.path.join(
            os.path.expanduser('~/Library/Application Support'),
            APP_DATA_FOLDER_NAME
        )
        os.makedirs(support_dir, exist_ok=True)
        return support_dir
    return os.path.dirname(sys.executable)


def get_install_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return base_dir


def migrate_runtime_data(install_dir, runtime_dir):
    if not getattr(sys, 'frozen', False):
        return
    if not install_dir or not runtime_dir:
        return
    install_real = os.path.realpath(install_dir)
    runtime_real = os.path.realpath(runtime_dir)
    if install_real == runtime_real or not os.path.isdir(install_real):
        return

    file_names = (
        'accounts.json',
        'accounts.json.bak',
        'results.csv',
        'upload.log',
        'app.log',
        'last-batch.csv',
    )
    dir_names = (
        'browser-profiles',
        'uploads',
        'screenshots',
        'webview-storage',
        'downloads',
        'data',
    )

    os.makedirs(runtime_real, exist_ok=True)

    for name in file_names:
        src = os.path.join(install_real, name)
        dst = os.path.join(runtime_real, name)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass

    for name in dir_names:
        src = os.path.join(install_real, name)
        dst = os.path.join(runtime_real, name)
        if not os.path.isdir(src) or os.path.exists(dst):
            continue
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        except OSError:
            pass

# 添加项目根目录到 Python 路径
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

# 扫描 site-packages 查找 webview DLL 目录
# frozen 模式下 _MEIPASS 内也有 webview/lib
if getattr(sys, 'frozen', False):
    _webview_lib = os.path.join(sys._MEIPASS, 'webview', 'lib')
    if os.path.isdir(_webview_lib):
        os.environ["PATH"] = _webview_lib + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, 'add_dll_directory'):
            try:
                os.add_dll_directory(_webview_lib)
            except Exception:
                pass
else:
    try:
        import site
        for scheme in site.getsitepackages():
            wv_path = os.path.join(scheme, "webview", "lib")
            if os.path.isdir(wv_path):
                os.environ["PATH"] = wv_path + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, 'add_dll_directory'):
                    try:
                        os.add_dll_directory(wv_path)
                    except Exception:
                        pass
                break
    except Exception:
        pass

# WebView2 稳定性标志，避免某些显卡/驱动导致 STATUS_BREAKPOINT 崩溃
os.environ.setdefault(
    'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
    '--disable-gpu-rasterization --disable-software-rasterizer'
)

# frozen (PyInstaller) vs 开发模式: 区分可写数据目录和只读资源目录
if getattr(sys, 'frozen', False):
    # 可写数据: Windows/macOS 都使用独立用户数据目录，避免被安装目录更新覆盖
    os.environ['APP_BASE_DIR'] = get_runtime_base_dir()
    os.environ['APP_INSTALL_DIR'] = get_install_dir()
    migrate_runtime_data(os.environ['APP_INSTALL_DIR'], os.environ['APP_BASE_DIR'])
    # 只读资源: PyInstaller 临时解压目录 (_internal/)
    os.environ['APP_RES_DIR'] = sys._MEIPASS
    # Playwright 浏览器: 优先检查 _internal/ms-playwright/
    # macOS 下可能在 Contents/MacOS/_internal/ms-playwright 或 Contents/Resources/ms-playwright
    candidates = [
        os.path.join(sys._MEIPASS, 'ms-playwright'),
        os.path.join(os.path.dirname(sys._MEIPASS), 'Resources', 'ms-playwright'),
    ]
    for _pw_browsers in candidates:
        if os.path.isdir(_pw_browsers):
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _pw_browsers
            break
else:
    os.environ.setdefault('APP_BASE_DIR', base_dir)
    os.environ.setdefault('APP_RES_DIR', base_dir)

os.environ.setdefault('APP_DATA_DIR', os.path.join(os.environ['APP_BASE_DIR'], 'data'))

# frozen 模式下没有控制台，将输出重定向到日志文件
if getattr(sys, 'frozen', False):
    _log_path = os.path.join(os.environ['APP_BASE_DIR'], 'app.log')
    sys.stdout = open(_log_path, 'a', encoding='utf-8')
    sys.stderr = sys.stdout

try:
    import main
    main.main()

except Exception:
    import traceback
    from datetime import datetime
    error_msg = traceback.format_exc()

    # 写入错误日志
    try:
        log_file = os.path.join(os.environ.get('APP_BASE_DIR', base_dir), "app.log")
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n[{0}] 启动失败:\n".format(datetime.now()))
            f.write(error_msg)
            f.write("\n")
    except Exception:
        pass

    print("=" * 60)
    print("启动失败！错误信息：")
    print("=" * 60)
    print(error_msg)
    print("=" * 60)

    if sys.stdin and sys.stdin.isatty():
        input("按回车键退出...")
