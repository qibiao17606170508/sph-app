#!/usr/bin/env python3
import json
import subprocess
import sys
import re


def fail(message):
    print(f"错误: {message}")
    sys.exit(1)

def run_command(cmd):
    print(f"执行命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        fail("命令执行失败。")


def run_capture(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
def get_current_version():
    try:
        with open("version.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("version", "1.0.0")
    except Exception:
        return "1.0.0"

def save_version(version):
    with open("version.json", "w", encoding="utf-8") as f:
        json.dump({"version": version}, f, indent=2)
        f.write("\n")


def validate_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail("版本号格式必须是 x.y.z，例如 1.0.13")


def ensure_git_repo_clean_enough():
    code, out, err = run_capture(["git", "status", "--porcelain"])
    if code != 0:
        fail(err or "无法读取 git 状态")

    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        return

    allowed = {"version.json", "release.py"}
    unexpected = []
    for line in lines:
        path = line[3:].strip()
        if path not in allowed:
            unexpected.append(path)

    if unexpected:
        print("检测到以下未提交改动：")
        for path in unexpected:
            print(f" - {path}")
        answer = input("继续发布会一并提交这些改动，是否继续？(y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            fail("已取消发布。")


def remote_tag_exists(tag_name):
    code, out, _err = run_capture(["git", "ls-remote", "--tags", "origin", tag_name])
    return code == 0 and bool(out.strip())


def local_browser_self_check():
    print("开始本地浏览器自检...")
    check_code = r'''
import asyncio
import os
import sys
from accounts import loadAccounts
from batch_upload import init_browser, unlock_profile

async def main():
    acct = loadAccounts()[0]
    profile_dir = acct["profileDir"]
    print(f"[自检] profileDir: {profile_dir}")
    print(f"[自检] exists: {os.path.exists(profile_dir)}")
    await unlock_profile(profile_dir)
    ctx = None
    try:
        ctx = await init_browser(profile_dir, headless=True)
        print(f"[自检] 浏览器启动成功，pages={len(ctx.pages)}")
    finally:
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-c", check_code], capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.strip())
        fail("本地浏览器自检失败，请先修复后再发布。")
    print("本地浏览器自检通过。")

def main():
    # 1. 获取当前版本
    ensure_git_repo_clean_enough()
    local_browser_self_check()

    current = get_current_version()
    print(f"当前版本号: {current}")


    # 2. 询问新版本号
    parts = current.split('.')
    if len(parts) == 3:
        suggested = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    else:
        suggested = current

    new_version = input(f"请输入新版本号 (回车默认为 {suggested}): ").strip()
    if not new_version:
        new_version = suggested
    validate_version(new_version)

    # 3. 更新 version.json
    tag_name = f"v{new_version}"
    if remote_tag_exists(tag_name):
        fail(f"远程标签 {tag_name} 已存在，请换一个新版本号。")

    save_version(new_version)
    print(f"已更新 version.json 为 {new_version}")

    # 4. Git 操作
    run_command(["git", "add", "."])

    code, _out, _err = run_capture(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        fail("没有可提交的改动。")

    run_command(["git", "commit", "-m", f"release: {tag_name}"])
    run_command(["git", "push", "origin", "main"])

    subprocess.run(["git", "tag", "-d", tag_name], capture_output=True)
    run_command(["git", "tag", tag_name])
    run_command(["git", "push", "origin", tag_name])

    print("\n" + "="*40)
    print(f"恭喜！版本 {tag_name} 已成功推送。")
    print("GitHub Actions 将自动开始打包并发布。")
    print(f"你可以前往 Actions 页面查看进度。")
    print("="*40)

if __name__ == "__main__":
    main()
