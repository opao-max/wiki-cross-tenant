#!/usr/bin/env python3
"""跨租户 wiki 迁移 · 前置自检

跑迁移之前先跑这个，会按顺序检查：
  1. lark-cli 已安装
  2. 两个 profile 存在 & token valid
  3. 两个 profile 已申请到所有必需 scope（协作者/外部访问/docx/评论等）
  4. 用真实文档做 API 烟雾测试（含添加/移除协作者，确保权限不只是申请到，还真能用）

任何一项失败 → 打印「缺什么 scope + 去哪里申请的直链」，并退出。
所有检查通过 → 报告「绿灯：可以开始迁移」。

用法:
    python3 preflight.py --source-profile seewo --target-profile xupt \\
        [--source-space 7085... --source-root Vg7e...] \\
        [--target-space 7633... --target-root MXja...] \\
        [--target-member-id ou_xxx...]

不传 space/root 也能跑，但只能查 scope 是否申请到，不会做烟雾测试。
不传 target-member-id 会跳过协作者烟雾测试。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import api, LarkCliError


GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"
DIM = "\033[2m"; BOLD = "\033[1m"; RESET = "\033[0m"

def ok(msg):    print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg):  print(f"  {RED}✗{RESET} {msg}")
def warn(msg):  print(f"  {YELLOW}!{RESET} {msg}")
def head(msg):  print(f"\n{BOLD}{msg}{RESET}")


# ---------------------------------------------------------------------------
# 必需的 scope 矩阵
# ---------------------------------------------------------------------------
# 每条 = (业务能力, [scope 候选...])
# 候选里任意一条在 token 里就算满足（飞书有粗/细粒度两种 scope，匹配任意一种即可）。
# 数据来自飞书开放平台错误返回的 permission_violations。

SCOPE_REQUIREMENTS = [
    ("列出/读取知识库节点", [
        "wiki:wiki",
        "wiki:wiki:readonly",
        "wiki:node:read",
        "wiki:node:retrieve",
        "wiki:space:read",
        "wiki:space:retrieve",
    ]),
    ("跨租户复制 wiki 节点", [
        "wiki:wiki",
        "wiki:node:copy",
    ]),
    ("添加协作者（跨租户 copy 强依赖）", [
        "docs:permission.member:create",
        "drive:drive",
        "drive:file",
    ]),
    ("移除协作者（copy 完后清理）", [
        "docs:permission.member:delete",
        "drive:drive",
        "drive:file",
    ]),
    ("查看协作者列表（验证/清理用）", [
        "docs:permission.member:retrieve",
        "drive:drive",
        "drive:file",
    ], False),  # 非致命
    ("读取权限设置（sync_permissions / 读 external_access 需要）", [
        "docs:permission.setting:read",
        "drive:drive",
        "drive:file",
    ], "source"),  # 仅源端需要
    ("临时开启外部访问（协作者被拒时 fallback）", [
        "docs:permission.setting:write_only",
        "drive:drive",
        "drive:file",
    ], "source"),  # 仅源端需要
    ("同步权限设置到目标端（copy 后写入源端权限）", [
        "docs:permission.setting:write_only",
        "drive:drive",
        "drive:file",
    ], "target"),  # 仅目标端需要
    ("读 docx 文档 blocks（链接重写需要）", [
        "docx:document",
        "docx:document:readonly",
        "docs:document.content:read",
    ]),
    ("改 docx 文档 blocks（链接重写写入）", [
        "docx:document",
        "docx:document:write_only",
    ]),
    ("读写多维表格记录（bitable 链接重写需要）", [
        "bitable:app",
        "bitable:app:readonly",
    ]),
    ("读电子表格（sheet 链接重写需要）", [
        "sheets:spreadsheet",
        "sheets:spreadsheet:read",
        "sheets:spreadsheet:readonly",
    ]),
    ("写电子表格（sheet 链接重写需要）", [
        "sheets:spreadsheet",
        "sheets:spreadsheet:write_only",
    ]),
    ("读评论（评论迁移需要）", [
        "drive:drive",
        "drive:file",
        "docs:document.comment:read",
    ]),
    ("写评论（评论迁移需要）", [
        "drive:drive",
        "docs:document.comment:create",
        "docs:document.comment:write_only",
    ]),
    ("解决/恢复评论（同步 is_solved 状态）", [
        "drive:drive",
        "docs:document.comment:update",
        "docs:document.comment:write_only",
    ], False),  # 非致命：不影响评论内容迁移，只影响 solved 状态同步
    ("读通讯录用户名（评论作者名展示，缺则显示「未知用户」）", [
        "contact:contact:readonly",
        "contact:contact.base:readonly",
        "contact:user.base:readonly",
        "contact:user.basic_profile:readonly",
    ], False),  # 第 3 个元素：False = 非致命
]


# 登录时需要显式传给 --scope 的完整列表（不传 lark-cli 不会带新 scope）
_SOURCE_SCOPES = "docs:permission.setting:write_only docs:permission.setting:read docs:permission.member:create docs:permission.member:delete docs:permission.member:retrieve docs:document.content:read docs:document.comment:read docs:document.comment:write_only docs:document.comment:create docs:document.comment:update docs:document.media:download docs:document.media:upload docx:document:readonly docx:document:write_only sheets:spreadsheet:read sheets:spreadsheet:write_only bitable:app wiki:wiki wiki:node:copy wiki:node:read wiki:node:retrieve wiki:space:read wiki:space:retrieve contact:user.base:readonly contact:user.basic_profile:readonly contact:contact.base:readonly offline_access"

_TARGET_SCOPES = "docs:permission.setting:write_only docs:document.content:read docs:document.comment:read docs:document.comment:write_only docs:document.comment:create docs:document.comment:update docs:document.media:download docs:document.media:upload docx:document:readonly docx:document:write_only sheets:spreadsheet:read sheets:spreadsheet:write_only bitable:app wiki:wiki wiki:node:copy wiki:node:read wiki:node:retrieve wiki:space:read wiki:space:retrieve contact:user.base:readonly contact:user.basic_profile:readonly contact:contact.base:readonly offline_access"


def _app_id_apply_url(app_id, scopes):
    if not app_id or not scopes:
        return ""
    return f"https://open.feishu.cn/app/{app_id}/auth?q={','.join(scopes)}&op_from=openapi"


# ---------------------------------------------------------------------------
# auth status：拿 scope / userOpenId
# ---------------------------------------------------------------------------

def auth_status(profile):
    """返回 dict 或 None。"""
    try:
        out = subprocess.run(
            ["lark-cli", "auth", "status", "--profile", profile],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def profile_list():
    try:
        out = subprocess.run(
            ["lark-cli", "profile", "list"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return json.loads(out)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 烟雾测试：用一个真实 obj_token 实际调一次关键 API
# ---------------------------------------------------------------------------

def sample_node_and_obj(profile, space_id, root_node):
    """返回 (node_token, obj_token) 样本。优先根节点。"""
    try:
        if root_node:
            d = api("GET", "/open-apis/wiki/v2/spaces/get_node",
                    params={"token": root_node}, profile=profile)
            n = d.get("node") or {}
            if n.get("obj_token") and n.get("node_token"):
                return n["node_token"], n["obj_token"]
        if space_id:
            d = api("GET", f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
                    params={"page_size": 10}, profile=profile)
            for n in (d.get("items") or []):
                if n.get("obj_type") == "docx" and n.get("obj_token") and n.get("node_token"):
                    return n["node_token"], n["obj_token"]
    except LarkCliError:
        pass
    return "", ""


def smoke_test(profile, app_id, node_token, obj_token, *, is_source, target_member_id=""):
    """对样本做实际 API 烟雾测试。返回 True 表示全过。"""
    all_pass = True

    # 1) 读 blocks
    try:
        api("GET", f"/open-apis/docx/v1/documents/{obj_token}/blocks",
            params={"page_size": 1}, profile=profile, timeout=20)
        ok("烟雾测试：读 docx blocks")
    except LarkCliError as e:
        if e.code in (99991679, 99991672):
            fail(f"烟雾测试：读 docx blocks — 缺 docx:document/docx:document:readonly")
            all_pass = False
        else:
            warn(f"烟雾测试：读 docx blocks — 非权限错误（{e}）")

    # 2) 添加/移除协作者（只在源端跑）
    if is_source and target_member_id and node_token:
        try:
            api("POST", f"/open-apis/drive/v1/permissions/{node_token}/members",
                params={"type": "wiki", "need_notification": "false"},
                data={"member_type": "openid", "member_id": target_member_id, "perm": "view"},
                profile=profile, timeout=20)
            ok("烟雾测试：添加协作者")
            # 立即移除
            try:
                api("DELETE", f"/open-apis/drive/v1/permissions/{node_token}/members/{target_member_id}",
                    params={"type": "wiki", "member_type": "openid"},
                    profile=profile, timeout=20)
                ok("烟雾测试：移除协作者")
            except LarkCliError as e:
                if e.code in (99991679, 99991672):
                    fail("烟雾测试：移除协作者 — 缺 docs:permission.member:delete")
                    all_pass = False
                else:
                    warn(f"烟雾测试：移除协作者 — 非权限错误（{e}）")
        except LarkCliError as e:
            if e.code in (99991679, 99991672):
                fail("烟雾测试：添加协作者 — 缺权限")
                if app_id:
                    print(f"      → 申请直链：{_app_id_apply_url(app_id, ['docs:permission.member:create', 'drive:drive'])}")
                all_pass = False
            else:
                warn(f"烟雾测试：添加协作者 — 非权限错误（{e}）")
    elif is_source and not target_member_id:
        warn("烟雾测试：跳过协作者测试（未传 --target-member-id）")

    # 3) 读评论
    try:
        api("GET", f"/open-apis/drive/v1/files/{obj_token}/comments",
            params={"file_type": "docx", "page_size": 1}, profile=profile, timeout=15)
        ok("烟雾测试：读评论")
    except LarkCliError as e:
        if e.code in (99991679, 99991672):
            warn(f"烟雾测试：读评论 — 缺权限，评论迁移会失败")
        else:
            warn(f"烟雾测试：读评论 — 非权限错误（{e}）")

    return all_pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def check_one_profile(label, profile, space_id, root_node, *, is_target, target_member_id=""):
    """返回 (pass: bool, failure_reason: str|None)。
    failure_reason ∈ {None, "no_profile", "token_expired", "missing_scope", "smoke_fail"}
    """
    head(f"{label}：profile = {profile}")

    # 1) 存在 + token valid
    plist = profile_list()
    pinfo = next((p for p in plist if p.get("name") == profile), None)
    if not pinfo:
        fail(f"profile「{profile}」不存在")
        scopes = _TARGET_SCOPES if is_target else _SOURCE_SCOPES
        print(f"\n  {BOLD}解决方法：{RESET}")
        print(f"  1. 创建 profile：")
        print(f"       lark-cli config init --name {profile}")
        print(f"  2. 登录（复制整条命令）：")
        print(f"       lark-cli auth login --profile {profile} --scope \"{scopes}\"")
        return False, "no_profile"

    app_id = pinfo.get("appId") or ""
    user_name = pinfo.get("user") or ""
    ok(f"profile 已存在  app_id={app_id}  user={user_name}")

    if pinfo.get("tokenStatus") != "valid":
        cur_status = pinfo.get("tokenStatus") or "unknown"
        scopes = _TARGET_SCOPES if is_target else _SOURCE_SCOPES
        fail(f"token 已过期（当前状态：{cur_status}）")
        print(f"\n  {BOLD}解决方法：{RESET}复制下面的命令到终端执行，重新登录即可：")
        print(f"       lark-cli auth login --profile {profile} --scope \"{scopes}\"")
        print(f"  浏览器打开后所有勾选项全部勾上，点授权。登录后再重跑本脚本。")
        return False, "token_expired"
    ok("token valid")

    # 2) 静态 scope 检查
    status = auth_status(profile)
    if not status:
        fail("无法读取 auth status，请确认 lark-cli 版本支持 `auth status`")
        return False, "token_expired"
    granted = set((status.get("scope") or "").split())
    open_id = status.get("userOpenId") or ""
    print(f"  {DIM}已授权 {len(granted)} 个 scope，user_open_id={open_id[:16]}…{RESET}")

    missing_fatal = []
    missing_warn = []
    for req in SCOPE_REQUIREMENTS:
        capability = req[0]
        candidates = req[1]
        third = req[2] if len(req) >= 3 else True
        if isinstance(third, str):
            side = third
            is_fatal = True
        else:
            side = None
            is_fatal = third

        if side == "source" and is_target:
            continue
        if side == "target" and not is_target:
            continue
        if is_target and "协作者" in capability:
            continue

        if any(c in granted for c in candidates):
            ok(f"scope ✓ {capability}")
        else:
            if is_fatal:
                fail(f"scope ✗ {capability}")
                missing_fatal.append((capability, candidates))
            else:
                warn(f"scope ! {capability}")
                missing_warn.append((capability, candidates))

    if missing_fatal:
        all_missing = []
        for _, cands in missing_fatal:
            all_missing.append(cands[0])
        all_missing = list(dict.fromkeys(all_missing))
        url = _app_id_apply_url(app_id, all_missing)
        scopes = _TARGET_SCOPES if is_target else _SOURCE_SCOPES
        print(f"\n  {BOLD}解决方法：{RESET}")
        print(f"  1. 在浏览器打开下面的链接，申请缺失的 scope：")
        print(f"       {url}")
        print(f"  2. 飞书开放平台后台 →「版本管理与发布」→ 创建版本 → 提交")
        print(f"     （企业自建应用一般秒过审批）")
        print(f"  3. 重新登录（复制整条命令，让新 scope 生效）：")
        print(f"       lark-cli auth login --profile {profile} --scope \"{scopes}\"")
        print(f"     浏览器打开后所有勾选项全部勾上，点授权。")
        print(f"  4. 再跑本脚本验证。")
        return False, "missing_scope"

    if missing_warn:
        print(f"  {YELLOW}有非致命 scope 缺失（迁移仍可跑，但部分功能降级）{RESET}")

    # 3) 烟雾测试
    node_token, obj_token = sample_node_and_obj(profile, space_id, root_node)
    if not obj_token:
        warn("没拿到样本 obj_token，跳过烟雾测试")
        warn("（建议传 --source-space + --source-root，或确保目标知识库有至少一篇 docx）")
        return True, None
    print(f"  {DIM}样本 node={node_token[:12]}… obj={obj_token[:12]}…{RESET}")
    passed = smoke_test(profile, app_id, node_token, obj_token,
                        is_source=not is_target, target_member_id=target_member_id)
    return passed, (None if passed else "smoke_fail")


def main():
    ap = argparse.ArgumentParser(
        description="迁移前置自检：检查 profile 配置 + 必备 scope + 烟雾测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--source-profile", required=True)
    ap.add_argument("--target-profile", required=True)
    ap.add_argument("--source-space", default="")
    ap.add_argument("--source-root",  default="")
    ap.add_argument("--target-space", default="")
    ap.add_argument("--target-root",  default="")
    ap.add_argument("--target-member-id", default="",
                    help="目标用户在源租户视角的 open_id（用于协作者烟雾测试）")
    args = ap.parse_args()

    print(f"{BOLD}迁移前置自检{RESET}  {DIM}preflight.py{RESET}")
    print(f"  源 profile: {args.source_profile}")
    print(f"  目标 profile: {args.target_profile}")

    # 0. lark-cli 安装
    head("0. lark-cli 安装")
    p = shutil.which("lark-cli")
    if not p:
        fail("找不到 lark-cli。请先安装。")
        sys.exit(2)
    ok(f"lark-cli 已安装：{p}")

    src_ok, src_reason = check_one_profile("源端", args.source_profile, args.source_space, args.source_root,
                               is_target=False, target_member_id=args.target_member_id)
    tgt_ok, tgt_reason = check_one_profile("目标端", args.target_profile, args.target_space, args.target_root,
                               is_target=True)

    print()
    if src_ok and tgt_ok:
        print(f"{GREEN}{BOLD}✓ 全部检查通过。可以开始迁移：{RESET}")
        print(f"  python3 sync.py --source-profile {args.source_profile} --target-profile {args.target_profile} \\")
        print(f"      --source-space {args.source_space or '<...>'} --source-root {args.source_root or '<...>'} \\")
        print(f"      --target-space {args.target_space or '<...>'} --target-root {args.target_root or '<...>'} \\")
        print(f"      --source-domain <src.feishu.cn> --target-domain <dst.feishu.cn> \\")
        print(f"      --target-member-id {args.target_member_id or '<TARGET_OPEN_ID>'}")
        sys.exit(0)
    else:
        print(f"{RED}{BOLD}✗ 自检未通过。{RESET}请按上面对应的「解决方法」操作后，重跑：")
        print(f"    python3 preflight.py --source-profile {args.source_profile} --target-profile {args.target_profile} \\")
        print(f"        --source-space {args.source_space or '<...>'} --target-space {args.target_space or '<...>'} \\")
        print(f"        --source-root {args.source_root or '<...>'} --target-root {args.target_root or '<...>'} \\")
        print(f"        --target-member-id {args.target_member_id or '<...>'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
