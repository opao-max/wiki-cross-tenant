#!/usr/bin/env python3
"""跨租户 wiki 迁移主脚本：添加协作者 + 服务端副本。

思路：
  1. BFS 源知识库节点（wiki/v2/spaces/{src_space}/nodes，按 parent_node_token 递归）
  2. 对每个源节点：添加目标用户为协作者（view 权限），绕过密码/分享限制
     如果添加协作者被 1063002 拒绝（外部访问未开启），临时开启 → copy → 恢复关闭
  3. 调用目标 profile 的 wiki/v2/spaces/{src_space}/nodes/{src_node}/copy
       data: {target_space_id, target_parent_token}
     不传 title → 标题保持 1:1
  4. 写入 mapping.json：node_mapping / obj_mapping / obj_type
  5. 复制完成后移除协作者（源文档不受任何影响）

子命令：
  scan        只列源节点 → state/source-tree.json，便于检查
  run         BFS + 添加协作者 + copy + 移除协作者（断点续传）
  cleanup     移除残留的协作者（迁移中断后清理用）
  status      打印 mapping 概览

约束：
  - 不暴露递归参数：API 没有；自己 BFS。
  - shortcut 节点：单独标 shortcut 类型，副本走默认行为（飞书会复制为 origin 副本，丢失 origin 引用）。
  - 父节点必须先成功 copy，子节点才能用 target_parent_token 挂上去。
  - 源文档的密码完全不变；外部访问如需临时开启会自动恢复。
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    api, api_paged, log, sleep_jitter, short,
    load_mapping, save_mapping, MAPPING_PATH, STATE_DIR,
    LarkCliError,
)

# ---------------------------------------------------------------------------
# 源端 BFS
# ---------------------------------------------------------------------------

def list_children(space_id, parent_token, profile):
    params = {"space_id": space_id}
    if parent_token:
        params["parent_node_token"] = parent_token
    return api_paged(
        "GET",
        f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
        params=params, profile=profile, item_key="items", page_size=50,
    )


def bfs_source(space_id, root_node, profile):
    """返回 [(depth, node_dict), ...]，按 BFS 顺序，根在前。"""
    out = []
    if root_node:
        # 拿根节点本身
        try:
            d = api(
                "GET",
                "/open-apis/wiki/v2/spaces/get_node",
                params={"token": root_node}, profile=profile,
            )
            root = d.get("node")
            if root:
                out.append((0, root))
                queue = [(0, root)]
            else:
                queue = []
        except LarkCliError as e:
            log(f"WARN get_node root failed: {e}")
            queue = []
    else:
        # 整个 space 的一级节点
        tops = list_children(space_id, None, profile)
        for n in tops:
            out.append((0, n))
        queue = [(0, n) for n in tops]

    while queue:
        depth, parent = queue.pop(0)
        if not parent.get("has_child"):
            continue
        try:
            children = list_children(space_id, parent["node_token"], profile)
        except LarkCliError as e:
            log(f"WARN list_children {parent.get('node_token')}: {e}")
            continue
        for c in children:
            out.append((depth + 1, c))
            queue.append((depth + 1, c))
        sleep_jitter(0.2)
    return out


# ---------------------------------------------------------------------------
# 分享 / 回收
# ---------------------------------------------------------------------------


def add_collaborator(node_token, member_open_id, profile):
    """添加目标用户为 wiki 节点协作者（view 权限），用于绕过密码/分享限制。"""
    return api(
        "POST",
        f"/open-apis/drive/v1/permissions/{node_token}/members",
        params={"type": "wiki", "need_notification": "false"},
        data={
            "member_type": "openid",
            "member_id": member_open_id,
            "perm": "view",
        },
        profile=profile,
    )


def remove_collaborator(node_token, member_open_id, profile):
    """移除协作者。"""
    return api(
        "DELETE",
        f"/open-apis/drive/v1/permissions/{node_token}/members/{member_open_id}",
        params={"type": "wiki", "member_type": "openid"},
        profile=profile,
    )


def read_external_access(node_token, profile):
    """读取当前的 external_access_entity 值。"""
    try:
        res = api(
            "GET",
            f"/open-apis/drive/v2/permissions/{node_token}/public",
            params={"type": "wiki"},
            profile=profile,
        )
        return (res.get("permission_public") or {}).get("external_access_entity") or ""
    except LarkCliError:
        return ""


def open_external_access(node_token, profile):
    """临时开启「允许内容被分享到组织外」。

    返回原始值（用于恢复），空字符串表示失败。
    仅在添加外部协作者被 1063002 拒绝时使用。
    不碰密码、不碰链接分享范围。
    """
    original = read_external_access(node_token, profile)
    try:
        api(
            "PATCH",
            f"/open-apis/drive/v2/permissions/{node_token}/public",
            params={"type": "wiki"},
            data={"external_access_entity": "open"},
            profile=profile,
        )
        return original or "closed"  # 记住原始值
    except LarkCliError:
        return ""


def restore_external_access(node_token, original_value, profile):
    """恢复 external_access_entity 到原始值。"""
    if not original_value or original_value == "open":
        return  # 原来就是 open 的不需要恢复
    try:
        api(
            "PATCH",
            f"/open-apis/drive/v2/permissions/{node_token}/public",
            params={"type": "wiki"},
            data={"external_access_entity": original_value},
            profile=profile,
        )
    except LarkCliError:
        pass


def sync_permissions(src_node, dst_node, src_profile, dst_profile):
    """把源端的公开权限同步到目标端（密码除外）。

    读取源端的 external_access_entity / link_share_entity / copy_entity /
    comment_entity / share_entity / manage_collaborator_entity / security_entity，
    写入目标端。不碰密码。
    """
    try:
        res = api(
            "GET",
            f"/open-apis/drive/v2/permissions/{src_node}/public",
            params={"type": "wiki"},
            profile=src_profile,
        )
        src_perm = res.get("permission_public") or {}
    except LarkCliError:
        return

    # 只同步这些字段（不含密码相关）
    fields = {}
    for key in ("external_access_entity", "link_share_entity", "copy_entity",
                "comment_entity", "share_entity", "manage_collaborator_entity",
                "security_entity"):
        if key in src_perm:
            fields[key] = src_perm[key]

    if not fields:
        return

    try:
        api(
            "PATCH",
            f"/open-apis/drive/v2/permissions/{dst_node}/public",
            params={"type": "wiki"},
            data=fields,
            profile=dst_profile,
        )
    except LarkCliError:
        pass


# ---------------------------------------------------------------------------
# 跨租户副本
# ---------------------------------------------------------------------------


def copy_node(src_space, src_node, target_space, target_parent, target_profile):
    data = {"target_space_id": target_space}
    if target_parent:
        data["target_parent_token"] = target_parent
    res = api(
        "POST",
        f"/open-apis/wiki/v2/spaces/{src_space}/nodes/{src_node}/copy",
        data=data, profile=target_profile,
    )
    return res.get("node") or {}


def move_node(space_id, node_token, target_parent_token, profile):
    """在同一知识库内移动节点（含子树）。"""
    return api(
        "POST",
        f"/open-apis/wiki/v2/spaces/{space_id}/nodes/{node_token}/move",
        data={"target_parent_token": target_parent_token},
        profile=profile,
    )


def _wait_bitable_ready(obj_token, profile, max_wait=300, interval=10):
    """轮询等待 bitable 异步复制完成。

    飞书跨租户 copy bitable 是异步的：API 返回成功但数据还在后台复制。
    如果在复制完成前恢复源端权限（关闭 external_access），后台进程就读不到源端，
    导致 bitable 永久损坏（1254002）。

    返回 True 表示就绪，False 表示超时。
    """
    import time as _time
    waited = 0
    while waited < max_wait:
        try:
            api("GET", f"/open-apis/bitable/v1/apps/{obj_token}",
                profile=profile, timeout=15)
            return True  # 可访问 = 复制完成
        except LarkCliError as e:
            if e.code == 1254036:
                # "Bitable is copying, please try again later"
                pass
            elif e.code == 1254002:
                # 还没准备好
                pass
            else:
                # 其他错误，不继续等
                log(f"  bitable wait: unexpected {e}")
                return False
        _time.sleep(interval)
        waited += interval
        if waited % 30 == 0:
            log(f"  bitable 异步复制中…已等 {waited}s")
    log(f"  bitable 等待超时 ({max_wait}s)，仍恢复源端权限")
    return False


# ---------------------------------------------------------------------------
# 增量同步辅助
# ---------------------------------------------------------------------------

def _get_or_create_trash(space_id, parent_token, profile):
    """获取或创建 _迁移回收站 节点。返回 node_token。"""
    # 先看 parent 下面有没有已经创建的
    try:
        children = list_children(space_id, parent_token, profile)
        for c in children:
            if c.get("title") == "_迁移回收站":
                return c["node_token"]
    except LarkCliError:
        pass
    # 创建
    res = api(
        "POST",
        f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
        data={
            "obj_type": "docx",
            "parent_node_token": parent_token,
            "title": "_迁移回收站",
        },
        profile=profile,
    )
    nt = (res.get("node") or {}).get("node_token") or ""
    if nt:
        log(f"  创建回收站节点: {nt}")
    return nt


def _list_target_children(space_id, parent_token, profile):
    """列出目标端某节点的所有直接子节点。"""
    try:
        return list_children(space_id, parent_token, profile)
    except LarkCliError:
        return []


def _classify_incremental(nodes, m):
    """对比新 BFS 和 mapping，分类出 new / disappeared / changed / moved。

    返回 (new_nodes, disappeared_src_nodes, changed_nodes, moved_nodes):
      new_nodes: [(depth, node_dict), ...]  — 不在 mapping 中的
      disappeared: [src_node_token, ...]    — 在 mapping 中但不在新 BFS 中
      changed: [(depth, node_dict), ...]    — 在 mapping 中且 edit_time 变了
      moved: [(depth, node_dict), ...]      — 在 mapping 中但 parent 变了（位置变了）
    """
    current_src_nodes = set()
    new_nodes = []
    changed_nodes = []
    moved_nodes = []

    for depth, n in nodes:
        nt = n.get("node_token") or ""
        if not nt:
            continue
        current_src_nodes.add(nt)
        if nt not in m["node_mapping"]:
            new_nodes.append((depth, n))
        else:
            # 已在 mapping 中，检查 edit_time 是否变化
            old_edit_time = m.get("edit_times", {}).get(nt)
            new_edit_time = n.get("obj_edit_time")
            if old_edit_time and new_edit_time and str(new_edit_time) != str(old_edit_time):
                changed_nodes.append((depth, n))

            # 检查父节点是否变化（位置移动）
            old_parent = m.get("parents", {}).get(nt, "")
            new_parent = n.get("parent_node_token") or ""
            if old_parent != new_parent:
                moved_nodes.append((depth, n))

    # 消失的节点：在 mapping 中但不在当前 BFS 中
    disappeared = []
    for src_nt in m["node_mapping"]:
        if src_nt not in current_src_nodes:
            disappeared.append(src_nt)

    return new_nodes, disappeared, changed_nodes, moved_nodes


def _find_top_level_disappeared(disappeared, m):
    """从消失节点中找顶层的（父节点未消失的）。
    子节点跟着父走，不需要单独处理。"""
    disappeared_set = set(disappeared)
    top_level = []
    for nt in disappeared:
        parent = m.get("parents", {}).get(nt, "")
        if parent not in disappeared_set:
            top_level.append(nt)
    return top_level


def _do_trash_nodes(top_disappeared, all_disappeared_set, m, target_space, trash_token, target_profile):
    """把消失的顶层节点 move 到回收站，并从 mapping 中清理。

    在 move 之前，先把目标端该节点下"还活着"的子节点（不在 all_disappeared_set 中的）
    移出来到该节点的父节点下，避免把它们误带进回收站。
    """
    trashed = 0
    for src_nt in top_disappeared:
        dst_nt = m["node_mapping"].get(src_nt, "")
        title = m.get("titles", {}).get(src_nt, "?")
        if not dst_nt:
            continue

        # 找 mapping 中 parents[x] == src_nt 且 x 不在 disappeared 中的子节点 → 还活着
        alive_children = []
        for child_src, parent_src in list(m.get("parents", {}).items()):
            if parent_src == src_nt and child_src not in all_disappeared_set:
                child_dst = m["node_mapping"].get(child_src, "")
                if child_dst:
                    alive_children.append((child_src, child_dst))

        # 把还活着的子节点从目标端移出来（移到 dst_nt 的父节点下）
        if alive_children:
            # 找 dst_nt 的父节点：通过 src_nt 的 parent 在 mapping 中找
            src_parent = m.get("parents", {}).get(src_nt, "")
            dst_parent = m["node_mapping"].get(src_parent, "") if src_parent else ""
            if not dst_parent:
                dst_parent = m["meta"].get("target_root_node", "")
            for child_src, child_dst in alive_children:
                try:
                    move_node(target_space, child_dst, dst_parent, target_profile)
                    log(f"  存活子节点移出: {short(m.get('titles', {}).get(child_src, '?'), 30)}")
                except LarkCliError as e:
                    log(f"  存活子节点移出失败: {child_dst} — {e}")
                sleep_jitter(0.2)

        try:
            move_node(target_space, dst_nt, trash_token, target_profile)
            log(f"  回收: {short(title, 40)} → _迁移回收站")
            trashed += 1
        except LarkCliError as e:
            log(f"  回收失败: {short(title, 40)} — {e}")
        sleep_jitter(0.3)

    # 从 mapping 中清理 —— 只清理真正消失的节点
    for nt in all_disappeared_set:
        m["node_mapping"].pop(nt, None)
        m["titles"].pop(nt, None)
        m["parents"].pop(nt, None)
        m.get("edit_times", {}).pop(nt, None)

    return trashed


def _do_update_node(depth, n, m, args, target_member_id, trash_token):
    """处理内容变更的节点：移子节点出 → 旧节点进回收站 → re-copy → 子节点移回。

    返回 True 表示成功。
    """
    src_nt = n.get("node_token") or ""
    src_ot = n.get("obj_token") or ""
    otype = n.get("obj_type") or ""
    title = short(n.get("title") or "(untitled)", 50)
    old_dst_nt = m["node_mapping"].get(src_nt, "")
    if not old_dst_nt:
        return False

    target_space = args.target_space
    target_profile = args.target_profile

    log(f"  更新: {otype} {title}")

    # 1) 找 old_dst_nt 在目标端的子节点，移到临时位置（old_dst_nt 的父节点）
    # 先找 old_dst_nt 的父节点（即这个节点在目标端应挂载的位置）
    src_parent = n.get("parent_node_token") or ""
    if src_nt == (args.source_root or ""):
        target_parent = m["meta"]["target_root_node"]
    elif src_parent and src_parent in m["node_mapping"]:
        target_parent = m["node_mapping"][src_parent]
    else:
        target_parent = m["meta"]["target_root_node"]

    # 列出旧目标节点的子节点
    dst_children = _list_target_children(target_space, old_dst_nt, target_profile)
    child_tokens = [c["node_token"] for c in dst_children if c.get("node_token")]

    # 把子节点暂时移到 target_parent（和旧节点同级）
    for ct in child_tokens:
        try:
            move_node(target_space, ct, target_parent, target_profile)
        except LarkCliError as e:
            log(f"    子节点移出失败 {ct}: {e}")
            return False
        sleep_jitter(0.2)

    if child_tokens:
        log(f"    {len(child_tokens)} 个子节点已移出")

    # 2) 旧节点移到回收站
    try:
        move_node(target_space, old_dst_nt, trash_token, target_profile)
        log(f"    旧节点已移入回收站")
    except LarkCliError as e:
        log(f"    旧节点回收失败: {e}")
        # 子节点已移出，但旧节点回收失败，尝试把子节点移回来
        for ct in child_tokens:
            try:
                move_node(target_space, ct, old_dst_nt, target_profile)
            except LarkCliError:
                pass
        return False

    # 3) 重新 copy（和全量 copy 一样的流程）
    original_external = ""
    if target_member_id:
        try:
            add_collaborator(src_nt, target_member_id, args.source_profile)
        except LarkCliError as e:
            if e.code == 1063002:
                log(f"    外部访问未开启，临时开启...")
                original_external = open_external_access(src_nt, args.source_profile)
                if original_external:
                    try:
                        add_collaborator(src_nt, target_member_id, args.source_profile)
                    except LarkCliError as e2:
                        log(f"    add-collaborator retry fail: {e2}")
                else:
                    log(f"    open external access fail")
            else:
                log(f"    add-collaborator warn: {e}")

    try:
        new_node = copy_node(
            args.source_space, src_nt, args.target_space, target_parent,
            args.target_profile,
        )
    except LarkCliError as e:
        log(f"    RE-COPY FAIL: {e}")
        if target_member_id:
            try:
                remove_collaborator(src_nt, target_member_id, args.source_profile)
            except LarkCliError:
                pass
        if original_external:
            restore_external_access(src_nt, original_external, args.source_profile)
        return False

    new_nt = new_node.get("node_token") or ""
    new_ot = new_node.get("obj_token") or ""
    new_type = new_node.get("obj_type") or ""

    if not new_nt or not new_ot:
        log(f"    RE-COPY EMPTY")
        if target_member_id:
            try:
                remove_collaborator(src_nt, target_member_id, args.source_profile)
            except LarkCliError:
                pass
        if original_external:
            restore_external_access(src_nt, original_external, args.source_profile)
        return False

    # bitable 等待
    if new_type == "bitable":
        _wait_bitable_ready(new_ot, args.target_profile)

    # 清理协作者 + 恢复外部访问
    if target_member_id:
        try:
            remove_collaborator(src_nt, target_member_id, args.source_profile)
        except LarkCliError as e:
            log(f"    remove-collaborator warn: {e}")
    if original_external:
        restore_external_access(src_nt, original_external, args.source_profile)
        log(f"    外部访问已恢复")

    # 同步权限
    sync_permissions(src_nt, new_nt, args.source_profile, args.target_profile)

    # 更新 mapping
    old_dst_nt = m["node_mapping"].get(src_nt, "")
    old_dst_ot = m["obj_mapping"].get(src_ot, "")
    m["node_mapping"][src_nt] = new_nt
    m["obj_mapping"][src_ot] = new_ot
    m["obj_type"][new_ot] = new_type
    m["edit_times"][src_nt] = str(n.get("obj_edit_time") or "")

    # 记录旧目标token → 新目标token的映射，供 fix-mentions 改写其他文档中的死链
    # 不能放在 node_mapping/obj_mapping 里，否则下次增量会误判为消失节点
    remap = m.setdefault("dst_remap", {})
    if old_dst_nt and old_dst_nt != new_nt:
        remap[old_dst_nt] = new_nt
    if old_dst_ot and old_dst_ot != new_ot:
        remap[old_dst_ot] = new_ot

    # 清除该文档的 comment_id_mapping，让评论重新迁移到新副本
    cid_map = m.get("comment_id_mapping", {})
    prefix = f"{src_ot}/"
    stale_keys = [k for k in cid_map if k.startswith(prefix)]
    for k in stale_keys:
        del cid_map[k]
    if stale_keys:
        log(f"    清除 {len(stale_keys)} 条旧评论映射，将重新迁移")

    log(f"    → node={new_nt} obj={new_ot}")

    # 4) 子节点移回新节点下
    for ct in child_tokens:
        try:
            move_node(target_space, ct, new_nt, target_profile)
        except LarkCliError as e:
            log(f"    子节点移回失败 {ct}: {e}")
        sleep_jitter(0.2)

    if child_tokens:
        log(f"    {len(child_tokens)} 个子节点已移回")

    return True


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    os.makedirs(STATE_DIR, exist_ok=True)
    log(f"BFS source space={args.source_space} root={args.source_root or '(top)'}")
    nodes = bfs_source(args.source_space, args.source_root, args.source_profile)
    log(f"got {len(nodes)} nodes")

    types = {}
    for _, n in nodes:
        t = n.get("obj_type", "?")
        types[t] = types.get(t, 0) + 1
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        log(f"  {t}: {c}")

    out_path = os.path.join(STATE_DIR, "source-tree.json")
    payload = {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source_space_id": args.source_space,
        "source_root": args.source_root,
        "count": len(nodes),
        "nodes": [
            {"depth": d, **{k: n.get(k) for k in (
                "node_token", "obj_token", "obj_type", "title",
                "parent_node_token", "node_type", "has_child",
                "origin_node_token", "origin_space_id",
                "obj_edit_time", "obj_create_time",
            )}}
            for d, n in nodes
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"saved {out_path}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def cmd_run(args):
    os.makedirs(STATE_DIR, exist_ok=True)
    m = load_mapping()

    # 写入 / 覆盖 meta
    meta = m["meta"]
    meta.update({
        "source_profile": args.source_profile,
        "target_profile": args.target_profile,
        "source_space_id": args.source_space,
        "target_space_id": args.target_space,
        "source_root_node": args.source_root or "",
        "target_root_node": args.target_root or "",
        "source_domain": args.source_domain or meta.get("source_domain", ""),
        "target_domain": args.target_domain or meta.get("target_domain", ""),
        "target_member_id": args.target_member_id or meta.get("target_member_id", ""),
    })
    save_mapping(m)

    # 1. BFS 源
    tree_path = os.path.join(STATE_DIR, "source-tree.json")
    if args.use_cached_tree and os.path.exists(tree_path):
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = json.load(f)
        nodes = [(n["depth"], n) for n in tree["nodes"]]
        log(f"loaded cached tree: {len(nodes)} nodes")
    else:
        log(f"BFS source space={args.source_space} root={args.source_root or '(top)'}")
        nodes = bfs_source(args.source_space, args.source_root, args.source_profile)
        log(f"got {len(nodes)} nodes")
        # 缓存一份
        with open(tree_path, "w", encoding="utf-8") as f:
            json.dump({
                "scanned_at": datetime.now().isoformat(timespec="seconds"),
                "source_space_id": args.source_space,
                "source_root": args.source_root,
                "count": len(nodes),
                "nodes": [{"depth": d, **n} for d, n in nodes],
            }, f, ensure_ascii=False, indent=2)

    # 按 depth 排序，父先于子
    nodes.sort(key=lambda x: x[0])

    # 获取目标用户在源租户的 open_id
    target_member_id = m["meta"].get("target_member_id", "")
    if not target_member_id:
        log("WARN: target_member_id 未设置，跳过协作者步骤（可能因密码导致 copy 失败）")

    # =====================================================================
    # 增量模式判断：mapping 中已有节点 → 增量；否则 → 全量
    # =====================================================================
    is_incremental = len(m.get("node_mapping", {})) > 0
    if is_incremental:
        log("=== 增量模式 ===")
        new_nodes, disappeared, changed_nodes, moved_nodes = _classify_incremental(nodes, m)
        log(f"分类结果: 新增={len(new_nodes)} 消失={len(disappeared)} 变更={len(changed_nodes)} 移动={len(moved_nodes)}")

        trash_token = ""
        needs_trash = len(disappeared) > 0 or len(changed_nodes) > 0

        if needs_trash:
            target_root = m["meta"]["target_root_node"] or ""
            trash_token = _get_or_create_trash(
                args.target_space, target_root, args.target_profile
            )
            if not trash_token:
                log("ERROR: 无法创建回收站节点，中止增量")
                return 1

        # --- 1. 处理消失节点 ---
        if disappeared:
            top_disappeared = _find_top_level_disappeared(disappeared, m)
            all_disappeared_set = set(disappeared)
            log(f"消失节点: 共 {len(disappeared)} 个，顶层 {len(top_disappeared)} 个")
            trashed = _do_trash_nodes(top_disappeared, all_disappeared_set, m, args.target_space, trash_token, args.target_profile)
            log(f"已回收 {trashed} 个顶层节点")
            save_mapping(m)

        # --- 2. 处理新增节点（先于移动，让新父节点进入 mapping）---
        if new_nodes:
            log(f"处理 {len(new_nodes)} 个新增节点")
        nodes_to_copy = new_nodes
    else:
        log("=== 全量模式 ===")
        nodes_to_copy = nodes

    # =====================================================================
    # 正常 copy 流程（全量时处理所有节点，增量时只处理新增）
    # =====================================================================
    total = len(nodes_to_copy)
    done_cnt = 0
    skipped_cnt = 0
    for idx, (depth, n) in enumerate(nodes_to_copy, 1):
        nt = n.get("node_token") or ""
        ot = n.get("obj_token") or ""
        otype = n.get("obj_type") or ""
        title = short(n.get("title") or "(untitled)", 50)
        psrc = n.get("parent_node_token") or ""

        if not nt or not ot:
            log(f"[{idx}/{total}] SKIP (missing token): {title}")
            continue

        if nt in m["node_mapping"]:
            skipped_cnt += 1
            continue

        # 父节点映射
        target_parent = ""
        if nt == args.source_root:
            target_parent = m["meta"]["target_root_node"]
        elif psrc and psrc in m["node_mapping"]:
            target_parent = m["node_mapping"][psrc]
        elif psrc:
            log(f"[{idx}/{total}] DEFER {otype}: {title} (parent {psrc[:8]} 未就绪)")
            _record_failure(m, n, "parent-missing", f"parent {psrc} not in mapping")
            continue
        else:
            target_parent = m["meta"]["target_root_node"]

        # 记录基础信息
        m["titles"][nt] = n.get("title") or ""
        m["parents"][nt] = psrc
        m["obj_type"][ot] = otype

        log(f"[{idx}/{total}] depth={depth} {otype}: {title}")

        # 1) 添加目标用户为协作者
        original_external = ""
        if target_member_id:
            try:
                add_collaborator(nt, target_member_id, args.source_profile)
            except LarkCliError as e:
                if e.code == 1063002:
                    log(f"  外部访问未开启，临时开启...")
                    original_external = open_external_access(nt, args.source_profile)
                    if original_external:
                        try:
                            add_collaborator(nt, target_member_id, args.source_profile)
                        except LarkCliError as e2:
                            log(f"  add-collaborator retry fail: {e2}")
                    else:
                        log(f"  open external access fail, copy 可能失败")
                else:
                    log(f"  add-collaborator warn: {e}")

        # 2) 复制
        try:
            new_node = copy_node(
                args.source_space, nt, args.target_space, target_parent,
                args.target_profile,
            )
        except LarkCliError as e:
            log(f"  COPY FAIL: {e}")
            _record_failure(m, n, "copy", str(e))
            if target_member_id:
                try:
                    remove_collaborator(nt, target_member_id, args.source_profile)
                except LarkCliError:
                    pass
            if original_external:
                restore_external_access(nt, original_external, args.source_profile)
            save_mapping(m)
            sleep_jitter(0.4)
            continue

        new_nt = new_node.get("node_token") or ""
        new_ot = new_node.get("obj_token") or ""
        new_type = new_node.get("obj_type") or ""

        if not new_nt or not new_ot:
            log(f"  COPY EMPTY: {new_node}")
            _record_failure(m, n, "copy-empty", json.dumps(new_node, ensure_ascii=False)[:200])
            if target_member_id:
                try:
                    remove_collaborator(nt, target_member_id, args.source_profile)
                except LarkCliError:
                    pass
            if original_external:
                restore_external_access(nt, original_external, args.source_profile)
            save_mapping(m)
            continue

        m["node_mapping"][nt] = new_nt
        m["obj_mapping"][ot] = new_ot
        m["obj_type"][new_ot] = new_type
        m["edit_times"][nt] = str(n.get("obj_edit_time") or "")
        log(f"  → node={new_nt} obj={new_ot} type={new_type}")

        # 2.5) bitable 异步等待
        if new_type == "bitable":
            _wait_bitable_ready(new_ot, args.target_profile)

        # 3) 清理
        if target_member_id:
            try:
                remove_collaborator(nt, target_member_id, args.source_profile)
            except LarkCliError as e:
                log(f"  remove-collaborator warn: {e}")
        if original_external:
            restore_external_access(nt, original_external, args.source_profile)
            log(f"  外部访问已恢复")

        # 4) 同步权限
        sync_permissions(nt, new_nt, args.source_profile, args.target_profile)

        done_cnt += 1
        if done_cnt % 10 == 0:
            save_mapping(m)
        sleep_jitter(0.4)

    save_mapping(m)

    # =====================================================================
    # 增量后续步骤：移动 → 变更（新增已在上面 copy 完，mapping 已更新）
    # =====================================================================
    if is_incremental:
        # --- 3. 处理移动节点（新增节点已 copy，mapping 中能找到新父节点）---
        if moved_nodes:
            changed_set = set(n.get("node_token") for _, n in changed_nodes)
            moved_only = [(d, n) for d, n in moved_nodes if n.get("node_token") not in changed_set]
            log(f"处理 {len(moved_only)} 个移动节点（排除 {len(moved_nodes)-len(moved_only)} 个同时变更的）")
            moved_cnt = 0
            for depth, n in moved_only:
                src_nt = n.get("node_token") or ""
                dst_nt = m["node_mapping"].get(src_nt, "")
                new_parent_src = n.get("parent_node_token") or ""
                title = short(n.get("title") or "", 40)
                if not dst_nt:
                    continue
                if new_parent_src and new_parent_src in m["node_mapping"]:
                    new_parent_dst = m["node_mapping"][new_parent_src]
                elif not new_parent_src or src_nt == (args.source_root or ""):
                    new_parent_dst = m["meta"]["target_root_node"]
                else:
                    log(f"  移动跳过: {title} (新父节点 {new_parent_src[:8]} 不在 mapping)")
                    continue
                try:
                    move_node(args.target_space, dst_nt, new_parent_dst, args.target_profile)
                    m["parents"][src_nt] = new_parent_src
                    log(f"  移动: {title} → 新位置")
                    moved_cnt += 1
                except LarkCliError as e:
                    log(f"  移动失败: {title} — {e}")
                sleep_jitter(0.3)
            log(f"移动完成: {moved_cnt}/{len(moved_only)}")
            save_mapping(m)

        # --- 4. 处理变更节点（浅→深）---
        if changed_nodes:
            changed_nodes.sort(key=lambda x: x[0])
            log(f"处理 {len(changed_nodes)} 个变更节点")
            updated = 0
            for depth, n in changed_nodes:
                ok = _do_update_node(depth, n, m, args, target_member_id, trash_token)
                if ok:
                    updated += 1
                save_mapping(m)
                sleep_jitter(0.4)
            log(f"更新完成: {updated}/{len(changed_nodes)}")

        log(f"增量完成. new_copied={done_cnt} disappeared={len(disappeared)} changed={len(changed_nodes)} moved={len(moved_nodes)} skipped={skipped_cnt}")
    else:
        log(f"done. copied={done_cnt} resumed_skip={skipped_cnt} failures={len(m['failures'])}")
    if m["failures"]:
        log("recent failures:")
        for f in m["failures"][-10:]:
            log(f"  {f['stage']}: {short(f['title'], 40)} — {short(f['error'], 80)}")
    return 0


def _record_failure(m, n, stage, error):
    m["failures"].append({
        "node_token": n.get("node_token"),
        "obj_token": n.get("obj_token"),
        "obj_type": n.get("obj_type"),
        "title": n.get("title") or "",
        "stage": stage,
        "error": error,
        "at": datetime.now().isoformat(timespec="seconds"),
    })


# ---------------------------------------------------------------------------
# cleanup（移除残留协作者）
# ---------------------------------------------------------------------------

def cmd_cleanup(args):
    m = load_mapping()
    src_profile = m["meta"].get("source_profile") or args.source_profile
    target_member_id = m["meta"].get("target_member_id", "")
    if not src_profile:
        log("ERROR: source_profile 未知")
        return 1
    if not target_member_id:
        log("ERROR: target_member_id 未知，无法清理协作者")
        return 1

    items = list(m["node_mapping"].keys())
    log(f"removing collaborator from {len(items)} source nodes via profile={src_profile}")
    removed, errs = 0, 0
    for nt in items:
        try:
            remove_collaborator(nt, target_member_id, src_profile)
            removed += 1
        except LarkCliError as e:
            errs += 1
            if errs <= 5:
                log(f"  remove fail {nt}: {e}")
        if removed % 20 == 0 and removed:
            log(f"  removed {removed}/{len(items)}")
        sleep_jitter(0.2)
    log(f"removed={removed} failed={errs}")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args):
    m = load_mapping()
    log(f"meta: {json.dumps(m['meta'], ensure_ascii=False)}")
    log(f"node_mapping: {len(m['node_mapping'])}")
    log(f"obj_mapping:  {len(m['obj_mapping'])}")
    log(f"failures:     {len(m['failures'])}")
    by_type = {}
    for ot, t in m["obj_type"].items():
        if ot in m["obj_mapping"]:
            by_type[t] = by_type.get(t, 0) + 1
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        log(f"  {t}: {c}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="wiki cross-tenant copy")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("scan", help="只 BFS 源，输出 source-tree.json")
    p.add_argument("--source-profile", required=True)
    p.add_argument("--source-space", required=True)
    p.add_argument("--source-root", default="", help="只扫某个子树（node_token）")

    p = sub.add_parser("run", help="添加协作者 + 跨租户 copy + 移除协作者")
    p.add_argument("--source-profile", required=True)
    p.add_argument("--target-profile", required=True)
    p.add_argument("--source-space", required=True)
    p.add_argument("--target-space", required=True)
    p.add_argument("--source-root", default="")
    p.add_argument("--target-root", default="", help="目标侧挂载父节点；空=顶级")
    p.add_argument("--source-domain", default="")
    p.add_argument("--target-domain", default="")
    p.add_argument("--target-member-id", default="",
                   help="目标用户在源租户视角的 open_id（协作者方案必须）")
    p.add_argument("--use-cached-tree", action="store_true")

    p = sub.add_parser("cleanup", help="移除残留的协作者（迁移中断后清理用）")
    p.add_argument("--source-profile", default="")

    p = sub.add_parser("status", help="打印 mapping 概览")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1

    return {
        "scan": cmd_scan,
        "run": cmd_run,
        "cleanup": cmd_cleanup,
        "status": cmd_status,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
