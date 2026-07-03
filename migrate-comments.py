#!/usr/bin/env python3
"""跨租户副本完成后，迁移源文档评论到目标文档。

策略：
  - 飞书 OpenAPI 直接发 comment 时 quote 字段会被服务端忽略（必为全文）
  - lark-cli drive +add-comment 提供高层封装：
      · --selection-with-ellipsis "<text>"  → 借 MCP locate-doc 把片段映射到 block_id
        → 服务端创建 anchor=block_id 的 local comment，能精准锚点
      · --full-comment                       → 全文评论
    所以本脚本的策略：源评论里 quote 非空且唯一就尝试锚点，否则降级全文。

正文格式：
    迁移者: <caller>
    原: <author_name>  (<source_time>)
    内容:
    <main reply text>

    回复 1 — <name> (<time>): <text>
    回复 2 — <name> (<time>): <text>
    ...

is_solved=true 的评论：迁完后再 PATCH /drive/v1/files/{file}/comments/{cid}?file_type=docx body={"is_solved": true}

用法：
  python3 migrate-comments.py [--dry-run]
  --dry-run 只统计源端有多少评论待迁，不实际迁移
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    api, api_paged, log, sleep_jitter, short,
    load_mapping, STATE_DIR, LarkCliError, lark_cli, make_url,
)


# ---------------------------------------------------------------------------
# 源端拉评论
# ---------------------------------------------------------------------------

def list_comments(file_token, file_type, profile):
    """主评论列表。"""
    return api_paged(
        "GET",
        f"/open-apis/drive/v1/files/{file_token}/comments",
        params={"file_type": file_type, "user_id_type": "open_id"},
        profile=profile, item_key="items", page_size=50,
    )


def list_replies(file_token, file_type, comment_id, profile):
    """评论的回复列表（有 has_more 时单独拉）。"""
    return api_paged(
        "GET",
        f"/open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies",
        params={"file_type": file_type, "user_id_type": "open_id"},
        profile=profile, item_key="items", page_size=50,
    )


# ---------------------------------------------------------------------------
# 用户名解析（缓存）
# ---------------------------------------------------------------------------

class UserNameCache:
    def __init__(self, profile):
        self.profile = profile
        self.cache = {}

    def get(self, open_id):
        if not open_id:
            return ""
        if open_id in self.cache:
            return self.cache[open_id]
        try:
            d = api(
                "GET", f"/open-apis/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
                profile=self.profile,
            )
            name = (d.get("user") or {}).get("name") or ""
        except LarkCliError:
            name = ""
        self.cache[open_id] = name
        return name


# ---------------------------------------------------------------------------
# 评论文本提取
# ---------------------------------------------------------------------------

def _extract_text(reply):
    """飞书评论 reply.content.elements 里抽纯文本。容忍字段为 None / 缺失。"""
    if not reply:
        return ""
    content = reply.get("content") or {}
    elems = content.get("elements") or []
    parts = []
    for e in elems:
        if not isinstance(e, dict):
            continue
        tr = e.get("text_run") if "text_run" in e else None
        dl = e.get("docs_link") if "docs_link" in e else None
        ps = e.get("person") if "person" in e else None
        if isinstance(tr, dict):
            parts.append(tr.get("text") or "")
        elif isinstance(dl, dict):
            parts.append(dl.get("url") or "")
        elif isinstance(ps, dict):
            parts.append(f"@{(ps.get('user_id') or '')[:8]}")
        else:
            # 兜底：忽略未知元素，避免污染正文
            continue
    return "".join(parts).strip()


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _build_body(comment, replies, src_users):
    """评论正文：仅 `姓名: 内容 (时间)`，多条回复换行接续。

    没有迁移者标注、没有幂等标记 —— 幂等改用 mapping 里的 comment_id_mapping。
    用户名缺失时显示"未知用户"，绝不暴露 open_id。
    """
    main = comment.get("reply_list", {}).get("replies") or []
    main_reply = main[0] if main else None
    extra = (main[1:] if main else []) + (replies or [])

    lines = []

    def _fmt(reply, fallback_time=None):
        name = src_users.get(reply.get("user_id")) or "未知用户" if reply else "未知用户"
        text = _extract_text(reply) if reply else ""
        ts = _fmt_time(reply.get("create_time")) if reply else _fmt_time(fallback_time)
        suffix = f" ({ts})" if ts else ""
        return f"{name}: {text}{suffix}".rstrip()

    if main_reply:
        lines.append(_fmt(main_reply, comment.get("create_time")))
    for r in extra:
        if not isinstance(r, dict):
            continue
        lines.append(_fmt(r))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 写评论：调 lark-cli drive +add-comment
# ---------------------------------------------------------------------------

def _add_comment(target_doc, body_text, profile, selection=None, block_id=None, dry_run=False):
    """调用 lark-cli drive +add-comment（仅 docx/doc）。

    优先 block_id 锚定 → selection 文本匹配 → --full-comment。
    返回 (ok, info_dict)
    """
    args = [
        "drive", "+add-comment",
        "--doc", target_doc,
        "--content", json.dumps([{"type": "text", "text": body_text}], ensure_ascii=False),
    ]
    if block_id:
        args += ["--block-id", block_id]
    elif selection:
        args += ["--selection-with-ellipsis", selection]
    else:
        args += ["--full-comment"]
    if dry_run:
        args += ["--dry-run"]
    try:
        res = lark_cli(args, profile=profile, as_user=True, timeout=60)
    except LarkCliError as e:
        return False, {"error": str(e)}
    if dry_run:
        return True, {"dry_run": True, "anchor_block_id": block_id or ""}
    if not res.get("ok"):
        return False, res
    return True, res.get("data") or {}


# ---------------------------------------------------------------------------
# Block 位置映射：源 block_id → 目标 block_id
# ---------------------------------------------------------------------------

def _build_block_map(src_obj, dst_obj, src_profile, dst_profile):
    """拉源/目标 blocks，按顺序建立 src_block_id → dst_block_id 映射。"""
    try:
        src_blocks = api_paged(
            "GET", f"/open-apis/docx/v1/documents/{src_obj}/blocks",
            params={}, profile=src_profile, item_key="items", page_size=500,
        )
        dst_blocks = api_paged(
            "GET", f"/open-apis/docx/v1/documents/{dst_obj}/blocks",
            params={}, profile=dst_profile, item_key="items", page_size=500,
        )
    except LarkCliError:
        return {}
    bmap = {}
    for i in range(min(len(src_blocks), len(dst_blocks))):
        sb = src_blocks[i].get("block_id", "")
        db = dst_blocks[i].get("block_id", "")
        if sb and db:
            bmap[sb] = db
    return bmap


def _extract_block_text(block):
    """提取 block 的纯文本内容（用于 quote 匹配）。"""
    bt = block.get("block_type")
    text_fields = block.get("text") or block.get("heading1") or block.get("heading2") or \
                  block.get("heading3") or block.get("heading4") or block.get("heading5") or \
                  block.get("heading6") or block.get("heading7") or block.get("heading8") or \
                  block.get("heading9") or block.get("bullet") or block.get("ordered") or \
                  block.get("code") or block.get("quote_container") or block.get("todo") or None
    if not text_fields:
        # heading/text blocks use block_type-specific key; try generic approach
        for key in ("text", "heading1", "heading2", "heading3", "heading4",
                    "heading5", "heading6", "heading7", "heading8", "heading9",
                    "bullet", "ordered", "code", "todo"):
            if key in block:
                text_fields = block[key]
                break
    if not text_fields or not isinstance(text_fields, dict):
        return ""
    parts = []
    for e in (text_fields.get("elements") or []):
        tr = e.get("text_run")
        if tr:
            parts.append(tr.get("content", "") or tr.get("text", ""))
        md = e.get("mention_doc")
        if md:
            parts.append(md.get("title", "") or "")
        mu = e.get("mention_user")
        if mu:
            parts.append(f"@{mu.get('user_id','')[:8]}")
        ps = e.get("person")
        if ps:
            parts.append(f"@{ps.get('user_id','')[:8]}")
    return "".join(parts)


def _find_block_for_quote(quote, src_blocks, comment_id=""):
    """在源端 blocks 里找 quote 所属的 block_id。

    策略优先级：
    1. 如果有 comment_id，直接从 block 的 comment_ids 字段匹配（最精确）
    2. quote 文本在 block content 里精确包含
    3. 前 20 字前缀匹配
    """
    # 方法1：comment_ids 字段直接匹配
    if comment_id:
        for b in src_blocks:
            if comment_id in (b.get("comment_ids") or []):
                return b.get("block_id", "")

    if not quote:
        return ""

    # 方法2：精确包含
    for b in src_blocks:
        txt = _extract_block_text(b)
        if quote in txt:
            return b.get("block_id", "")

    # 方法3：前 20 字匹配
    prefix = quote[:20]
    for b in src_blocks:
        txt = _extract_block_text(b)
        if prefix in txt:
            return b.get("block_id", "")
    return ""


def _patch_solved(target_doc, comment_id, profile):
    return api(
        "PATCH",
        f"/open-apis/drive/v1/files/{target_doc}/comments/{comment_id}",
        params={"file_type": "docx"},
        data={"is_solved": True},
        profile=profile,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _comment_targets(mapping):
    """要迁评论的 (src_obj, dst_obj, src_type, dst_type) —— docx 优先。
    旧 doc copy 后会变 docx，但源评论得用源类型查。
    """
    out = []
    for src, dst in mapping["obj_mapping"].items():
        src_type = mapping["obj_type"].get(src) or mapping["obj_type"].get(dst) or ""
        dst_type = mapping["obj_type"].get(dst) or src_type
        # 评论 API 支持的类型
        if src_type not in ("docx", "doc"):
            continue
        out.append((src, dst, src_type, dst_type))
    return out


def _unsupported_comment_targets(mapping):
    """不能迁评论但能读评论的类型：sheet, bitable, file, slides。"""
    out = []
    for src, dst in mapping["obj_mapping"].items():
        src_type = mapping["obj_type"].get(src) or mapping["obj_type"].get(dst) or ""
        dst_type = mapping["obj_type"].get(dst) or src_type
        if src_type in ("sheet", "bitable", "file", "slides"):
            out.append((src, dst, src_type, dst_type))
    return out


def _build_obj_to_node(mapping):
    """构建 obj_token → node_token 反查表（源端和目标端都建）。"""
    import os as _os
    obj_to_node = {}
    # 从 source-tree.json
    tree_path = _os.path.join(STATE_DIR, "source-tree.json")
    if _os.path.exists(tree_path):
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = json.load(f)
        for n in tree.get("nodes", []):
            ot = n.get("obj_token") or ""
            nt = n.get("node_token") or ""
            if ot and nt:
                obj_to_node[ot] = nt
    # 目标端：通过 node_mapping + obj_mapping 反推
    # node_mapping: {src_node: dst_node}, obj_mapping: {src_obj: dst_obj}
    for src_obj, dst_obj in mapping["obj_mapping"].items():
        if src_obj in obj_to_node:
            src_node = obj_to_node[src_obj]
            dst_node = mapping["node_mapping"].get(src_node, "")
            if dst_node:
                obj_to_node[dst_obj] = dst_node
    return obj_to_node


def _obj_to_wiki_url(obj_token, mapping, domain, obj_to_node):
    """obj_token → wiki URL（用 node_token）。"""
    node = obj_to_node.get(obj_token, "")
    if node:
        return make_url(domain, "wiki", node)
    # fallback: 用 obj_type 直接构造（可能打不开但至少有信息）
    obj_type = mapping["obj_type"].get(obj_token, "docx")
    return make_url(domain, obj_type, obj_token)


def _scan_unsupported_comments(mapping, src_profile, src_users, obj_to_node):
    """扫描 sheet/bitable 等不支持写入的评论，返回报告列表。"""
    targets = _unsupported_comment_targets(mapping)
    src_domain = mapping["meta"].get("source_domain", "")
    dst_domain = mapping["meta"].get("target_domain", "")
    results = []

    for src, dst, stype, dtype in targets:
        try:
            comments = list_comments(src, stype, src_profile)
        except LarkCliError:
            continue
        if not comments:
            continue

        comment_details = []
        for c in comments:
            all_replies = c.get("reply_list", {}).get("replies") or []
            for r in all_replies:
                images = (r.get("extra") or {}).get("image_list") or []
                author = src_users.get(r.get("user_id")) or "未知用户"
                comment_details.append({
                    "quote": (c.get("quote") or "")[:80],
                    "text": (_extract_text(r) or "")[:200],
                    "author": author,
                    "time": _fmt_time(r.get("create_time")),
                    "has_image": bool(images),
                    "image_tokens": images,
                })

        if comment_details:
            results.append({
                "src_url": _obj_to_wiki_url(src, mapping, src_domain, obj_to_node),
                "dst_url": _obj_to_wiki_url(dst, mapping, dst_domain, obj_to_node),
                "src_obj": src,
                "dst_obj": dst,
                "src_type": stype,
                "comment_count": len(comments),
                "reply_count": len(comment_details),
                "comments": comment_details[:50],
            })
        sleep_jitter(0.2)

    return results


def _seed_existing_mapping(m, targets, tgt_profile):
    """一次性把目标文档里已存在的旧格式评论（含 [migrated:xxx] 标记）回收进 cid_map。

    早期版本在评论正文末尾插过这种幂等标记。新版本改为正文清爽 + 用 mapping 持久化，
    所以重跑前先扫一次目标，把旧标记抓出来塞进 cid_map，避免重复发送。
    扫完不删旧评论 —— 用户要的是不要重复，正文长得旧的留着也无所谓（后续可手动清）。
    """
    cid_map = m.setdefault("comment_id_mapping", {})
    PFX, SFX = "[migrated:", "]"
    seeded = 0
    for src, dst, _stype, dtype in targets:
        try:
            existing = list_comments(dst, dtype, tgt_profile)
        except LarkCliError:
            continue
        for c in existing:
            dst_cid = c.get("comment_id")
            for r in (c.get("reply_list", {}).get("replies") or []):
                txt = _extract_text(r) or ""
                i = txt.rfind(PFX)
                if i < 0:
                    continue
                j = txt.find(SFX, i + len(PFX))
                if j < 0:
                    continue
                src_cid = txt[i + len(PFX):j].strip()
                if not src_cid:
                    continue
                key = f"{src}/{src_cid}"
                if key not in cid_map and dst_cid:
                    cid_map[key] = dst_cid
                    seeded += 1
                break
    if seeded:
        from common import save_mapping
        save_mapping(m)
        log(f"seeded {seeded} comment id mappings from old [migrated:] markers")
    return seeded


def _run(args, dry_run):
    m = load_mapping()
    src_profile = m["meta"].get("source_profile") or args.source_profile
    tgt_profile = m["meta"].get("target_profile") or args.target_profile
    if not src_profile or not tgt_profile:
        log("ERROR: source/target profile 未知")
        return 1

    src_users = UserNameCache(src_profile)
    cid_map = m.setdefault("comment_id_mapping", {})
    obj_to_node = _build_obj_to_node(m)

    targets = _comment_targets(m)
    log(f"docs with possible comments: {len(targets)}  dry_run={dry_run}")

    # 一次性回收旧版 [migrated:xxx] 标记到 cid_map（无副作用，重跑安全）
    if not dry_run:
        _seed_existing_mapping(m, targets, tgt_profile)

    counter = {
        "docs_with_comments": 0,
        "comments_total": 0,
        "comments_anchored": 0,
        "comments_full": 0,
        "comments_fallback_full": 0,  # 锚点失败回退到全文
        "comments_failed": 0,
        "comments_skipped_dup": 0,
        "is_solved_synced": 0,
        "images_skipped": 0,
    }
    failures = []
    fallbacks = []  # 锚点失败回退全文的列表
    images_skipped = []  # docx 评论迁了文本但丢了图片

    for idx, (src, dst, stype, dtype) in enumerate(targets, 1):
        try:
            comments = list_comments(src, stype, src_profile)
        except LarkCliError as e:
            log(f"[{idx}/{len(targets)}] list_comments fail {src}: {e}")
            continue
        if not comments:
            continue

        counter["docs_with_comments"] += 1
        log(f"[{idx}/{len(targets)}] {short(src,12)}→{short(dst,12)} comments={len(comments)}")

        # 对 docx 文档建立 block 位置映射
        block_map = {}
        src_blocks = []
        if stype in ("docx", "doc") and dtype in ("docx", "doc"):
            block_map = _build_block_map(src, dst, src_profile, tgt_profile)
            if block_map:
                try:
                    src_blocks = api_paged(
                        "GET", f"/open-apis/docx/v1/documents/{src}/blocks",
                        params={}, profile=src_profile, item_key="items", page_size=500,
                    )
                except LarkCliError:
                    src_blocks = []

        for c in comments:
            counter["comments_total"] += 1
            src_cid = c.get("comment_id") or ""
            cid_key = f"{src}/{src_cid}"
            if src_cid and cid_key in cid_map:
                counter["comments_skipped_dup"] += 1
                continue
            quote = (c.get("quote") or "").strip()
            is_whole = bool(c.get("is_whole"))

            # 拉取所有回复（如果 has_more）
            replies = []
            if c.get("has_more"):
                try:
                    replies = list_replies(src, stype, c["comment_id"], src_profile)
                except LarkCliError as e:
                    log(f"  list_replies fail {c['comment_id']}: {e}")

            # 缓存涉及到的用户名
            user_ids = set()
            for r in (c.get("reply_list", {}).get("replies") or []) + replies:
                if r.get("user_id"):
                    user_ids.add(r["user_id"])
            user_name_map = {uid: src_users.get(uid) for uid in user_ids}

            body = _build_body(c, replies, user_name_map)

            # 收集评论中的图片 token（迁移时会丢失）
            all_replies_for_img = (c.get("reply_list", {}).get("replies") or []) + replies
            comment_images = []
            for r in all_replies_for_img:
                imgs = (r.get("extra") or {}).get("image_list") or []
                if imgs:
                    comment_images.extend(imgs)

            # 决定锚点：优先 block_id 精确映射
            target_block_id = ""
            sel = None
            if not is_whole and block_map and src_blocks:
                src_bid = _find_block_for_quote(quote, src_blocks, comment_id=src_cid)
                if src_bid and src_bid in block_map:
                    target_block_id = block_map[src_bid]

            # 没找到 block_id 时退回文本匹配
            if not target_block_id and not is_whole and quote:
                if len(quote) <= 60:
                    sel = quote
                else:
                    sel = quote[:30] + "..." + quote[-20:]

            ok, info = _add_comment(
                dst, body, tgt_profile, selection=sel,
                block_id=target_block_id, dry_run=dry_run,
            )

            new_cid = None
            if ok:
                if info.get("anchor_block_id") or target_block_id:
                    counter["comments_anchored"] += 1
                else:
                    counter["comments_full"] += 1
                new_cid = info.get("comment_id")
            else:
                # block_id 或文本锚点失败 → 退回文本匹配 → 再失败退全文
                if target_block_id:
                    # block_id 失败，试文本匹配
                    sel_fallback = None
                    if quote:
                        sel_fallback = quote if len(quote) <= 60 else quote[:30] + "..." + quote[-20:]
                    ok2, info2 = _add_comment(dst, body, tgt_profile, selection=sel_fallback, dry_run=dry_run)
                    if ok2:
                        counter["comments_anchored"] += 1
                        new_cid = info2.get("comment_id")
                    else:
                        # 最终退全文
                        ok3, info3 = _add_comment(dst, body, tgt_profile, selection=None, dry_run=dry_run)
                        if ok3:
                            counter["comments_fallback_full"] += 1
                            new_cid = info3.get("comment_id")
                            fallbacks.append({
                                "src_doc": src, "dst_doc": dst, "src_cid": src_cid,
                                "quote": short(quote, 60),
                            })
                        else:
                            counter["comments_failed"] += 1
                            failures.append({
                                "src_doc": src, "dst_doc": dst, "src_cid": src_cid,
                                "stage": "all-fallback", "error": str(info3)[:200],
                            })
                elif sel:
                    ok2, info2 = _add_comment(dst, body, tgt_profile, selection=None, dry_run=dry_run)
                    if ok2:
                        counter["comments_fallback_full"] += 1
                        new_cid = info2.get("comment_id")
                        fallbacks.append({
                            "src_doc": src, "dst_doc": dst, "src_cid": src_cid,
                            "quote": short(quote, 60),
                        })
                    else:
                        counter["comments_failed"] += 1
                        failures.append({
                            "src_doc": src, "dst_doc": dst, "src_cid": src_cid,
                            "stage": "fallback-full", "error": str(info2)[:200],
                        })
                else:
                    counter["comments_failed"] += 1
                    failures.append({
                        "src_doc": src, "dst_doc": dst, "src_cid": src_cid,
                        "stage": "full", "error": str(info)[:200],
                    })

            # 记录 cid 映射；is_solved 同步；图片丢失记录
            if new_cid and not dry_run:
                if src_cid:
                    cid_map[cid_key] = new_cid
                if c.get("is_solved"):
                    try:
                        _patch_solved(dst, new_cid, tgt_profile)
                        counter["is_solved_synced"] += 1
                    except LarkCliError as e:
                        log(f"  solved patch fail {new_cid}: {e}")

            # 记录丢失的图片（无论 dry_run 与否都记）
            if comment_images:
                src_domain = m["meta"].get("source_domain", "")
                dst_domain = m["meta"].get("target_domain", "")
                counter["images_skipped"] += len(comment_images)
                images_skipped.append({
                    "src_url": _obj_to_wiki_url(src, m, src_domain, obj_to_node),
                    "dst_url": _obj_to_wiki_url(dst, m, dst_domain, obj_to_node),
                    "src_cid": src_cid,
                    "comment_text": short(body, 100),
                    "image_tokens": comment_images,
                })

            sleep_jitter(0.25)

        # 每篇文档持久化一次 mapping，崩了不丢已迁的 cid
        if not dry_run:
            from common import save_mapping
            save_mapping(m)

        sleep_jitter(0.2)

    # 扫描不支持写入的类型的评论（sheet/bitable 等）
    log("scanning unsupported types (sheet/bitable/...) for report...")
    unsupported = _scan_unsupported_comments(m, src_profile, src_users, obj_to_node)
    unsupported_count = sum(u["reply_count"] for u in unsupported)
    if unsupported:
        log(f"  unsupported types: {len(unsupported)} docs, {unsupported_count} replies (not migrated)")

    # 报告
    out = os.path.join(STATE_DIR, "comments-report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "ran_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "summary": counter,
            "failures": failures[:300],
            "fallbacks": fallbacks[:300],
            "images_skipped": images_skipped[:300],
            "unsupported_comments": unsupported[:200],
        }, f, ensure_ascii=False, indent=2)
    log(f"summary: {json.dumps(counter, ensure_ascii=False)}")
    log(f"saved {out}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="migrate comments to copied docs (with anchor when possible)")
    ap.add_argument("--source-profile", default="")
    ap.add_argument("--target-profile", default="")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = ap.parse_args()
    return _run(args, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
