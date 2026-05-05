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

base_dir = os.path.dirname(os.path.abspath(__file__))


def get_runtime_base_dir():
    if not getattr(sys, 'frozen', False):
        return base_dir
    if sys.platform == 'darwin':
        support_dir = os.path.join(
            os.path.expanduser('~/Library/Application Support'),
            '视频号批量上传'
        )
        os.makedirs(support_dir, exist_ok=True)
        return support_dir
    return os.path.dirname(sys.executable)

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
    # 可写数据: Windows 用 exe 所在目录；macOS 用 Application Support
    os.environ['APP_BASE_DIR'] = get_runtime_base_dir()
    # 只读资源: PyInstaller 临时解压目录 (_internal/)
    os.environ['APP_RES_DIR'] = sys._MEIPASS
    # Playwright 浏览器: 捆绑在 _internal/ms-playwright/ 下
    _pw_browsers = os.path.join(sys._MEIPASS, 'ms-playwright')
    if os.path.isdir(_pw_browsers):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _pw_browsers
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
