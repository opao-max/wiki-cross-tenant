#!/usr/bin/env python3
"""一键迁移：主迁移 + 链接改写 + 评论迁移 + 生成报告。

全量或增量自动判断（有 mapping.json = 增量）。
任何一步失败立即停止。

用法：
  python3 sync.py \
    --source-profile seewo --target-profile xupt \
    --source-space <SRC> --target-space <DST> \
    --source-root <ROOT> --target-root <ROOT> \
    --source-domain agqg3o3wxu.feishu.cn \
    --target-domain rcnwnx20zrwi.feishu.cn \
    --target-member-id <ou_xxx>
"""

import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

GREEN = "\033[32m"; RED = "\033[31m"; BOLD = "\033[1m"; DIM = "\033[2m"; RESET = "\033[0m"


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def run_step(name, cmd):
    print(f"\n{BOLD}[{_ts()}] === {name} ==={RESET}", flush=True)
    print(f"{DIM}$ {' '.join(cmd)}{RESET}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"{RED}[{_ts()}] {name} 失败 (exit={result.returncode})，中止。{RESET}", flush=True)
        sys.exit(result.returncode)
    print(f"{GREEN}[{_ts()}] {name} 完成{RESET}", flush=True)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="一键迁移：copy + fix-mentions + comments + report")
    ap.add_argument("--source-profile", required=True)
    ap.add_argument("--target-profile", required=True)
    ap.add_argument("--source-space", required=True)
    ap.add_argument("--target-space", required=True)
    ap.add_argument("--source-root", default="")
    ap.add_argument("--target-root", default="")
    ap.add_argument("--source-domain", default="")
    ap.add_argument("--target-domain", default="")
    ap.add_argument("--target-member-id", default="")
    ap.add_argument("--use-cached-tree", action="store_true")
    args = ap.parse_args()

    py = sys.executable

    # 1. 主迁移
    copy_cmd = [
        py, "share-copy.py", "run",
        "--source-profile", args.source_profile,
        "--target-profile", args.target_profile,
        "--source-space", args.source_space,
        "--target-space", args.target_space,
    ]
    if args.source_root:
        copy_cmd += ["--source-root", args.source_root]
    if args.target_root:
        copy_cmd += ["--target-root", args.target_root]
    if args.source_domain:
        copy_cmd += ["--source-domain", args.source_domain]
    if args.target_domain:
        copy_cmd += ["--target-domain", args.target_domain]
    if args.target_member_id:
        copy_cmd += ["--target-member-id", args.target_member_id]
    if args.use_cached_tree:
        copy_cmd += ["--use-cached-tree"]

    run_step("主迁移 (share-copy)", copy_cmd)

    # 2. 链接改写
    run_step("链接改写 (fix-mentions)", [py, "fix-mentions.py"])

    # 3. 评论迁移
    run_step("评论迁移 (migrate-comments)", [py, "migrate-comments.py"])

    # 4. 生成报告
    run_step("生成报告 (report)", [py, "report.py"])

    print(f"\n{GREEN}{BOLD}[{_ts()}] 全部完成。{RESET}")
    print(f"  报告: open state/report.html")


if __name__ == "__main__":
    main()
