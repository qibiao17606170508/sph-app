#!/usr/bin/env python3
import json
import os
import subprocess
import sys

def run_command(cmd):
    print(f"执行命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"错误: 命令执行失败。")
        sys.exit(1)

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

def main():
    # 1. 获取当前版本
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

    # 3. 更新 version.json
    save_version(new_version)
    print(f"已更新 version.json 为 {new_version}")

    # 4. Git 操作
    tag_name = f"v{new_version}"
    
    # git add
    run_command(["git", "add", "."])
    
    # git commit
    run_command(["git", "commit", "-m", f"release: {tag_name}"])
    
    # git push origin main
    run_command(["git", "push", "origin", "main"])
    
    # git tag
    # 先删除本地同名 tag 防止冲突 (可选)
    subprocess.run(["git", "tag", "-d", tag_name], capture_output=True)
    run_command(["git", "tag", tag_name])
    
    # git push origin tag
    run_command(["git", "push", "origin", tag_name])

    print("\n" + "="*40)
    print(f"恭喜！版本 {tag_name} 已成功推送。")
    print("GitHub Actions 将自动开始打包并发布。")
    print(f"你可以前往 Actions 页面查看进度。")
    print("="*40)

if __name__ == "__main__":
    main()
