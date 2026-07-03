#!/usr/bin/env python3
"""跨租户副本完成后，重写目标文档里指向源租户的 @文档 / 链接。

输入：state/mapping.json（含 node_mapping, obj_mapping, obj_type, meta.target_domain）

处理：
  - 遍历每个目标 docx（type == 'docx'）的 blocks
  - 在带 elements 的 block 字段里（text/heading{1-9}/bullet/ordered/quote/todo/callout）
    找到：
      · element.mention_doc.token 命中 obj_mapping → 改写 token 与 obj_type
      · element.text_run.text_element_style.link.url 含 wiki/docx/... 且 token 命中 mapping
        → 改写 URL（替换 token，可选替换 domain）
  - 按 block 调 PATCH /open-apis/docx/v1/documents/{doc}/blocks/{block}
    body: { "update_text_elements": {"elements": [...], "style": ...} } （或对应 heading/bullet 字段）
  - 越界外链（域名是飞书但 token 不在 mapping）：保留并写报告

用法：
  python3 fix-mentions.py [--dry-run]
  --dry-run 只统计要改写多少个，不实际写入
"""

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    api, api_paged, log, sleep_jitter, short,
    load_mapping, STATE_DIR,
    parse_lark_url, make_url, LarkCliError,
)

# 带文本 elements 的 block 字段
_TEXT_FIELDS = (
    "text", "heading1", "heading2", "heading3", "heading4", "heading5",
    "heading6", "heading7", "heading8", "heading9",
    "bullet", "ordered", "quote", "todo", "callout",
    "code", "equation",
)


def list_blocks(doc_token, profile):
    return api_paged(
        "GET",
        f"/open-apis/docx/v1/documents/{doc_token}/blocks",
        profile=profile, item_key="items", page_size=500,
    )


def _rewrite_url(url, mapping, target_domain):
    """返回 (new_url, hit_kind)，hit_kind ∈ rewrite|external|miss|notlark"""
    parsed = parse_lark_url(url)
    if not parsed:
        return url, "notlark"
    domain, kind, token = parsed

    if kind == "wiki":
        if token in mapping["node_mapping"]:
            new_token = mapping["node_mapping"][token]
            new_dom = target_domain or domain
            return make_url(new_dom, "wiki", new_token), "rewrite"
        return url, "external"

    # 其它都是 obj_token
    if token in mapping["obj_mapping"]:
        new_token = mapping["obj_mapping"][token]
        # 用映射后的 obj_type 决定 URL 段
        new_kind = mapping["obj_type"].get(new_token, kind)
        new_dom = target_domain or domain
        return make_url(new_dom, new_kind, new_token), "rewrite"
    return url, "external"


def _rewrite_elements(elements, mapping, target_domain, counter):
    """就地构造新 elements；返回 (changed, new_elements)。"""
    changed = False
    new_elems = []
    for elem in elements:
        e = elem  # default keep

        if "mention_doc" in elem and isinstance(elem["mention_doc"], dict):
            md = elem["mention_doc"]
            tok = md.get("token") or ""
            # mention_doc.token 可能是 obj_token 也可能是 node_token
            if tok in mapping["obj_mapping"]:
                new_tok = mapping["obj_mapping"][tok]
                new_type = mapping["obj_type"].get(new_tok, md.get("obj_type"))
                e = json.loads(json.dumps(elem))  # deep copy
                e["mention_doc"]["token"] = new_tok
                if new_type:
                    e["mention_doc"]["obj_type"] = _OBJ_TYPE_NUM.get(new_type, md.get("obj_type"))
                changed = True
                counter["mention_doc_rewrite"] += 1
            elif tok in mapping["node_mapping"]:
                new_tok = mapping["node_mapping"][tok]
                e = json.loads(json.dumps(elem))  # deep copy
                e["mention_doc"]["token"] = new_tok
                changed = True
                counter["mention_doc_rewrite"] += 1
            elif tok:
                counter["mention_doc_external"] += 1

        elif "text_run" in elem and isinstance(elem["text_run"], dict):
            tr = elem["text_run"]
            link = (tr.get("text_element_style") or {}).get("link") or {}
            url = link.get("url") or ""
            if url:
                # url 是 percent-encoded
                try:
                    decoded = urllib.parse.unquote(url)
                except Exception:
                    decoded = url
                new_url, hit = _rewrite_url(decoded, mapping, target_domain)
                if hit == "rewrite":
                    e = json.loads(json.dumps(elem))
                    e["text_run"]["text_element_style"]["link"]["url"] = urllib.parse.quote(new_url, safe=":/?&=#%")
                    changed = True
                    counter["link_rewrite"] += 1
                elif hit == "external":
                    counter["link_external"] += 1
                    counter["external_urls"].append(decoded)

        new_elems.append(e)
    return changed, new_elems


# mention_doc.obj_type 在飞书内部是数值常量。常见映射（保守起见，找不到就回原值）
_OBJ_TYPE_NUM = {
    "doc": 1,
    "sheet": 3,
    "bitable": 8,
    "mindnote": 11,
    "file": 12,
    "slides": 15,
    "wiki": 16,
    "docx": 22,
}


def _build_src_obj_to_dst_node(mapping):
    """构建 src_obj_token → dst_node_token 映射。

    mapping 结构：
      node_mapping: {src_node: dst_node}
      obj_mapping:  {src_obj: dst_obj}
    需要：src_obj → dst_node（用于 reference_base 和 mention_doc 改写）

    方法：从 source-tree.json 的节点数据中，每个节点有 node_token + obj_token，
    所以 src_node → src_obj 是一一对应的。反过来 src_obj → src_node → dst_node。
    """
    # 先建 src_obj → src_node 反查
    # 但我们没有直接数据…用 node_mapping + obj_mapping 间接：
    # 如果 src_node 在 node_mapping 中，且 src_obj 在 obj_mapping 中，
    # 那么两者是同一个文档的两个 token。
    # 但 mapping 中没有直接的 node→obj 关联！
    # 解法：从 state/source-tree.json 读（如果有），否则用 titles/parents 间接。
    # 最简单：遍历 source-tree.json
    import os as _os
    tree_path = _os.path.join(STATE_DIR, "source-tree.json")
    src_obj_to_dst_node = {}
    if _os.path.exists(tree_path):
        with open(tree_path, "r", encoding="utf-8") as f:
            tree = json.load(f)
        for n in tree.get("nodes", []):
            src_nt = n.get("node_token") or ""
            src_ot = n.get("obj_token") or ""
            if src_nt and src_ot and src_nt in mapping["node_mapping"]:
                dst_node = mapping["node_mapping"][src_nt]
                src_obj_to_dst_node[src_ot] = dst_node
    return src_obj_to_dst_node


def _collect_reference_base_rewrites(src_obj, dst_obj, mapping, src_obj_to_dst_node, target_profile, source_profile):
    """处理 reference_base (block_type=53) 改写。

    飞书跨租户 copy 时对 reference_base 的行为不一致：有时保留（指向源端，无权限），有时丢弃。
    策略：
      1. 目标端如果仍有 reference_base block → 删除它
      2. 如果还没插过对应的 mention_doc → 在对应位置插入

    返回 (to_delete, to_insert):
      to_delete: [(child_index_in_target, block_id), ...]
      to_insert: [(insert_index_in_target, old_token, new_node_token), ...]
    """
    # 拉源端 blocks
    try:
        src_blocks = api_paged(
            "GET", f"/open-apis/docx/v1/documents/{src_obj}/blocks",
            profile=source_profile, item_key="items", page_size=500,
        )
    except LarkCliError:
        return [], []

    # 找源端的 reference_base 位置和 token
    src_refs = []  # [(child_index_in_src, token, dst_node)]
    child_idx = 0
    for b in src_blocks:
        if b.get("block_type") == 1:
            continue
        if b.get("block_type") == 53:
            ref = b.get("reference_base") or {}
            token = ref.get("token") or ""
            base_token = token.split("_")[0] if "_" in token else token
            if base_token in src_obj_to_dst_node:
                src_refs.append((child_idx, token, src_obj_to_dst_node[base_token]))
        child_idx += 1

    if not src_refs:
        return [], []

    # 拉目标端 blocks
    try:
        dst_blocks = api_paged(
            "GET", f"/open-apis/docx/v1/documents/{dst_obj}/blocks",
            profile=target_profile, item_key="items", page_size=500,
        )
    except LarkCliError:
        return [], []

    # 1) 找目标端残留的 reference_base → 需要删除
    to_delete = []
    dst_child_idx = 0
    for b in dst_blocks:
        if b.get("block_type") == 1:
            continue
        if b.get("block_type") == 53:
            to_delete.append((dst_child_idx, b["block_id"]))
        dst_child_idx += 1

    # 2) 检查目标端已有的 mention_doc（幂等）
    existing_mentions = set()
    for b in dst_blocks:
        if b.get("block_type") == 2:
            for elem in (b.get("text") or {}).get("elements") or []:
                md = elem.get("mention_doc")
                if md:
                    existing_mentions.add(md.get("token", ""))

    # 3) 决定哪些需要插入
    to_insert = []
    ref_count_before = 0
    for src_idx, token, dst_node in src_refs:
        dst_obj_tok = mapping["obj_mapping"].get(
            token.split("_")[0] if "_" in token else token, ""
        )
        if dst_node in existing_mentions or dst_obj_tok in existing_mentions:
            ref_count_before += 1
            continue  # 已经插过了
        # 插入位置：源端位置减去丢失的 ref_base 数，再减去即将被删除的数
        insert_idx = src_idx - ref_count_before
        to_insert.append((insert_idx, token, dst_node))
        ref_count_before += 1

    return to_delete, to_insert


def _collect_block_updates(blocks, mapping, target_domain, counter):
    """返回 list of (block_id, field_name, payload_dict)。

    所有文本类 block（text/heading*/bullet/quote/todo/callout/...）的 PATCH
    都共用 key `update_text_elements`，按 block_id 定位即可，无需按字段拼名。
    """
    updates = []
    for b in blocks:
        for field in _TEXT_FIELDS:
            content = b.get(field)
            if not isinstance(content, dict):
                continue
            elements = content.get("elements")
            if not isinstance(elements, list) or not elements:
                continue
            changed, new_elems = _rewrite_elements(elements, mapping, target_domain, counter)
            if changed:
                style = content.get("style") or {}
                req = {
                    "block_id": b["block_id"],
                    "update_text_elements": {
                        "elements": new_elems,
                        "style": style,
                    },
                }
                updates.append((b["block_id"], field, req))
                break  # 一个 block 只属于一个文本字段
    return updates


def _patch_blocks_batch(doc_token, requests, profile):
    """batch_update 端点。requests 是若干 update_text_elements 请求。"""
    return api(
        "PATCH",
        f"/open-apis/docx/v1/documents/{doc_token}/blocks/batch_update",
        data={"requests": requests}, profile=profile,
    )


def _delete_block(doc_token, block_id, start_index, profile):
    """从文档的 page block 中删除指定位置的子 block。"""
    # 先拿 page block（document_id == page block_id）
    return api(
        "DELETE",
        f"/open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token}/children/batch_delete",
        data={"start_index": start_index, "end_index": start_index + 1},
        profile=profile,
    )


def _insert_mention_doc_block(doc_token, index, target_node_token, obj_type, profile):
    """在文档 page block 的指定位置插入一个含 mention_doc 的 text block。

    mention_doc 渲染为 @文档 卡片，比 text_run+link 美观。
    统一使用 obj_type=16(wiki) + node_token，这样无论实际文档类型如何都能正确渲染。
    """
    block_data = {
        "block_type": 2,  # text block
        "text": {
            "elements": [
                {
                    "mention_doc": {
                        "token": target_node_token,
                        "obj_type": 16,  # wiki — 用 node_token 时必须是 16
                        "mention_type": 1,  # 1 = doc mention
                    }
                }
            ],
            "style": {},
        },
    }
    return api(
        "POST",
        f"/open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token}/children",
        data={"children": [block_data], "index": index},
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Sheet 链接改写
# ---------------------------------------------------------------------------

def _list_sheet_ids(obj_token, profile):
    """返回 [(sheet_id, title), ...]。"""
    data = api(
        "GET",
        f"/open-apis/sheets/v2/spreadsheets/{obj_token}/metainfo",
        profile=profile,
    )
    return [
        (s.get("sheetId"), s.get("title") or "")
        for s in (data.get("sheets") or [])
        if s.get("sheetId")
    ]


def _read_sheet_values(obj_token, sheet_id, profile):
    """读取一个工作表的全部值。返回二维数组。"""
    data = api(
        "GET",
        f"/open-apis/sheets/v2/spreadsheets/{obj_token}/values/{sheet_id}",
        profile=profile,
    )
    return (data.get("valueRange") or {}).get("values") or []


def _rewrite_sheet_cell(cell, mapping, target_domain, counter):
    """改写 sheet cell 中的飞书链接。cell 是 list of segments。
    返回 (changed, new_cell)。

    注意：sheet 写入 API 不支持 mention 格式（报 90204 invalid email），
    所以 mention 改写会降级为纯文本 URL。整个 cell 变成字符串。
    """
    if not isinstance(cell, list):
        return False, cell
    changed = False
    new_parts = []
    for seg in cell:
        if not isinstance(seg, dict):
            new_parts.append(str(seg))
            continue
        seg_type = seg.get("type")
        link = seg.get("link") or ""
        text = seg.get("text") or ""

        if seg_type == "mention" and link:
            parsed = parse_lark_url(link)
            if not parsed:
                new_parts.append(text)
                continue
            domain, kind, token = parsed
            anchor = ""
            if "#" in link:
                anchor = "#" + link.split("#", 1)[1]

            new_token, hit = _resolve_token(token, kind, mapping)
            if hit == "rewrite":
                new_dom = target_domain or domain
                new_kind = mapping["obj_type"].get(new_token, kind)
                new_url = make_url(new_dom, new_kind, new_token) + anchor
                new_parts.append(new_url)
                changed = True
                counter["sheet_mention_rewrite"] += 1
            else:
                new_parts.append(text)
                if hit == "external":
                    counter["sheet_mention_external"] += 1

        elif seg_type == "url" and link:
            new_url, hit = _rewrite_url(link, mapping, target_domain)
            if hit == "rewrite":
                new_parts.append(new_url)
                changed = True
                counter["sheet_url_rewrite"] += 1
            else:
                new_parts.append(text or link)
                if hit == "external":
                    counter["sheet_url_external"] += 1
        else:
            new_parts.append(text)

    if changed:
        return True, "".join(new_parts)
    return False, cell


def _resolve_token(token, kind, mapping):
    """根据 token 和 kind 查 mapping，返回 (new_token, hit_kind)。
    hit_kind ∈ rewrite|external|miss
    """
    if kind == "wiki":
        if token in mapping["node_mapping"]:
            return mapping["node_mapping"][token], "rewrite"
        return token, "external"
    # obj token
    if token in mapping["obj_mapping"]:
        return mapping["obj_mapping"][token], "rewrite"
    return token, "external"


def _write_sheet_cell(obj_token, sheet_id, row_idx, col_idx, cell_value, profile):
    """写回单个 cell。row_idx/col_idx 都是 0-based。"""
    def col_letter(n):
        s = ""
        while True:
            s = chr(ord("A") + n % 26) + s
            n = n // 26 - 1
            if n < 0:
                break
        return s
    col = col_letter(col_idx)
    r = row_idx + 1  # 1-based
    range_str = f"{sheet_id}!{col}{r}:{col}{r}"
    return api(
        "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{obj_token}/values",
        data={
            "valueRange": {
                "range": range_str,
                "values": [[cell_value]],
            }
        },
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Bitable 链接改写
# ---------------------------------------------------------------------------

def _list_bitable_tables(app_token, profile):
    """返回 [(table_id, name), ...]。"""
    items = api_paged(
        "GET",
        f"/open-apis/bitable/v1/apps/{app_token}/tables",
        profile=profile, item_key="items", page_size=100,
    )
    return [(t.get("table_id"), t.get("name") or "") for t in items if t.get("table_id")]


def _list_bitable_fields(app_token, table_id, profile):
    """返回 [(field_id, field_name, field_type), ...]。"""
    items = api_paged(
        "GET",
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        profile=profile, item_key="items", page_size=100,
    )
    return [(f.get("field_id"), f.get("field_name") or "", f.get("type")) for f in items if f.get("field_id")]


def _list_bitable_records(app_token, table_id, profile):
    """返回全部 record。text_field_as_array=true 使 text 字段返回 segments 数组。"""
    return api_paged(
        "GET",
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
        params={"text_field_as_array": "true"},
        profile=profile, item_key="items", page_size=500,
    )


def _rewrite_bitable_text_segments(segments, mapping, target_domain, counter):
    """改写 bitable text 字段中的 segments（和 sheet cell 结构类似）。
    segments 是 list of dicts，每个可能有 type=url/mention + link。
    返回 (changed, new_segments)。
    """
    if not isinstance(segments, list):
        return False, segments
    changed = False
    new_segs = []
    for seg in segments:
        if not isinstance(seg, dict):
            new_segs.append(seg)
            continue

        seg_type = seg.get("type")
        link = seg.get("link") or ""

        if seg_type == "url" and link:
            new_url, hit = _rewrite_url(link, mapping, target_domain)
            if hit == "rewrite":
                new_seg = dict(seg)
                new_seg["link"] = new_url
                new_segs.append(new_seg)
                changed = True
                counter["bitable_url_rewrite"] += 1
            else:
                new_segs.append(seg)
                if hit == "external":
                    counter["bitable_url_external"] += 1
        elif seg_type == "mention" and link:
            parsed = parse_lark_url(link)
            if not parsed:
                new_segs.append(seg)
                continue
            domain, kind, token = parsed
            anchor = ""
            if "#" in link:
                anchor = "#" + link.split("#", 1)[1]
            new_token, hit = _resolve_token(token, kind, mapping)
            if hit == "rewrite":
                new_seg = dict(seg)
                new_dom = target_domain or domain
                new_kind = mapping["obj_type"].get(new_token, kind)
                if "token" in new_seg:
                    new_seg["token"] = new_token
                new_seg["link"] = make_url(new_dom, new_kind, new_token) + anchor
                new_segs.append(new_seg)
                changed = True
                counter["bitable_mention_rewrite"] += 1
            else:
                new_segs.append(seg)
                if hit == "external":
                    counter["bitable_mention_external"] += 1
        else:
            new_segs.append(seg)
    return changed, new_segs


def _rewrite_bitable_hyperlink(val, mapping, target_domain, counter):
    """改写 bitable hyperlink 字段（type=15）。值为 {"text": "...", "link": "..."}。
    返回 (changed, new_val)。
    """
    if not isinstance(val, dict) or not val.get("link"):
        return False, val
    link = val["link"]
    new_url, hit = _rewrite_url(link, mapping, target_domain)
    if hit == "rewrite":
        new_val = dict(val)
        new_val["link"] = new_url
        counter["bitable_url_rewrite"] += 1
        return True, new_val
    if hit == "external":
        counter["bitable_url_external"] += 1
    return False, val


def _rewrite_bitable_record(record, link_fields, mapping, target_domain, counter):
    """改写一条 record 中的链接字段。
    link_fields: [(field_id, field_name, field_type), ...] 只含需要检查的字段。
    返回 (changed, update_fields) — update_fields 只含变化的字段。

    注意：bitable text 字段（type=1）写入时只能传纯字符串，不能传 segments 数组。
    所以 mention 改写会降级为纯文本 URL（丢失卡片样式，但链接正确）。
    """
    fields = record.get("fields") or {}
    changed = False
    update_fields = {}

    for fid, fname, ftype in link_fields:
        val = fields.get(fname)
        if val is None:
            continue

        if ftype == 1:
            # text 字段：读取时是 segments 数组（text_field_as_array=true），
            # 但写入只能传字符串。改写 mention → 纯文本 URL。
            if isinstance(val, list):
                any_changed = False
                new_parts = []
                for seg in val:
                    if not isinstance(seg, dict):
                        new_parts.append(str(seg))
                        continue
                    seg_type = seg.get("type")
                    link = seg.get("link") or ""
                    text = seg.get("text") or ""

                    if seg_type == "mention" and link:
                        parsed = parse_lark_url(link)
                        if parsed:
                            domain, kind, token = parsed
                            anchor = ""
                            if "#" in link:
                                anchor = "#" + link.split("#", 1)[1]
                            new_token, hit = _resolve_token(token, kind, mapping)
                            if hit == "rewrite":
                                new_dom = target_domain or domain
                                new_kind = mapping["obj_type"].get(new_token, kind)
                                new_url = make_url(new_dom, new_kind, new_token) + anchor
                                new_parts.append(new_url)
                                any_changed = True
                                counter["bitable_mention_rewrite"] += 1
                            else:
                                new_parts.append(text or link)
                                if hit == "external":
                                    counter["bitable_mention_external"] += 1
                        else:
                            new_parts.append(text)
                    elif seg_type == "url" and link:
                        new_url, hit = _rewrite_url(link, mapping, target_domain)
                        if hit == "rewrite":
                            new_parts.append(new_url)
                            any_changed = True
                            counter["bitable_url_rewrite"] += 1
                        else:
                            new_parts.append(text or link)
                            if hit == "external":
                                counter["bitable_url_external"] += 1
                    else:
                        new_parts.append(text)
                if any_changed:
                    update_fields[fname] = "".join(new_parts)
                    changed = True
            elif isinstance(val, str):
                # 纯文本字符串，无 segments，跳过
                pass
        elif ftype == 15:
            # hyperlink 字段
            c, new_val = _rewrite_bitable_hyperlink(val, mapping, target_domain, counter)
            if c:
                update_fields[fname] = new_val
                changed = True

    return changed, update_fields


def _update_bitable_record(app_token, table_id, record_id, fields, profile):
    """更新一条 record 的指定字段。"""
    return api(
        "PUT",
        f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        data={"fields": fields},
        profile=profile,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _targets_by_type(mapping, obj_type):
    """目标侧需要扫描的文档：按 obj_type 过滤。"""
    out = []
    for src_obj, dst_obj in mapping["obj_mapping"].items():
        t = mapping["obj_type"].get(dst_obj) or mapping["obj_type"].get(src_obj) or ""
        if t == obj_type:
            out.append((src_obj, dst_obj))
    return out


def _docx_targets(mapping):
    return _targets_by_type(mapping, "docx")


def _sheet_targets(mapping):
    return _targets_by_type(mapping, "sheet")


def _bitable_targets(mapping):
    return _targets_by_type(mapping, "bitable")



def _run(args, dry_run):
    m = load_mapping()
    target_profile = m["meta"].get("target_profile") or args.target_profile
    target_domain = m["meta"].get("target_domain") or args.target_domain or ""
    if not target_profile:
        log("ERROR: target_profile 未知")
        return 1

    # 把 dst_remap 合并进 node_mapping/obj_mapping，让旧目标端 token 也能被改写
    # （增量 re-copy 后旧目标token变死链，需要改写为新目标token）
    dst_remap = m.get("dst_remap") or {}
    if dst_remap:
        for old_tok, new_tok in dst_remap.items():
            if old_tok not in m["node_mapping"]:
                m["node_mapping"][old_tok] = new_tok
            if old_tok not in m["obj_mapping"]:
                m["obj_mapping"][old_tok] = new_tok
        log(f"dst_remap: 合并 {len(dst_remap)} 条旧→新目标token映射")

    docs = _docx_targets(m)
    log(f"docx targets: {len(docs)}  (dry_run={dry_run})")

    # 构建 src_obj → dst_node 映射（reference_base 改写用）
    src_obj_to_dst_node = _build_src_obj_to_dst_node(m)

    counter = {
        "blocks_scanned": 0,
        "blocks_changed": 0,
        "mention_doc_rewrite": 0,
        "mention_doc_external": 0,
        "link_rewrite": 0,
        "link_external": 0,
        "ref_base_rewrite": 0,
        "ref_base_fail": 0,
        "patch_ok": 0,
        "patch_fail": 0,
        "external_urls": [],
        "patch_errors": [],
        "sheets_scanned": 0,
        "sheets_changed": 0,
        "sheet_mention_rewrite": 0,
        "sheet_mention_external": 0,
        "sheet_url_rewrite": 0,
        "sheet_url_external": 0,
        "bitables_scanned": 0,
        "bitable_records_changed": 0,
        "bitable_url_rewrite": 0,
        "bitable_url_external": 0,
        "bitable_mention_rewrite": 0,
        "bitable_mention_external": 0,
    }

    per_doc = []
    for idx, (src, dst) in enumerate(docs, 1):
        try:
            blocks = list_blocks(dst, target_profile)
        except LarkCliError as e:
            log(f"[{idx}/{len(docs)}] FAIL list_blocks {dst}: {e}")
            counter["patch_fail"] += 1
            counter["patch_errors"].append({"doc": dst, "stage": "list", "error": str(e)})
            continue

        counter["blocks_scanned"] += len(blocks)

        # 1) 文本类 block 改写（mention_doc / link）
        updates = _collect_block_updates(blocks, m, target_domain, counter)

        # 2) reference_base (block_type=53) 改写：对比源端和目标端
        source_profile = m["meta"].get("source_profile") or ""
        ref_deletes, ref_inserts = _collect_reference_base_rewrites(
            src, dst, m, src_obj_to_dst_node, target_profile, source_profile
        ) if source_profile else ([], [])
        ref_count = len(ref_deletes) + len(ref_inserts)

        if not updates and not ref_count:
            continue

        text_count = len(updates)
        log(f"[{idx}/{len(docs)}] {short(dst,16)} → text={text_count} ref_del={len(ref_deletes)} ref_ins={len(ref_inserts)}")
        per_doc.append({"src": src, "dst": dst, "blocks_changed": text_count, "ref_base": ref_count})

        if dry_run:
            counter["blocks_changed"] += text_count
            counter["ref_base_rewrite"] += ref_count
            continue

        # apply 文本改写
        for bid, field, req in updates:
            try:
                _patch_blocks_batch(dst, [req], target_profile)
                counter["patch_ok"] += 1
                counter["blocks_changed"] += 1
            except LarkCliError as e:
                counter["patch_fail"] += 1
                counter["patch_errors"].append({
                    "doc": dst, "src_doc": src, "block": bid, "field": field, "error": str(e),
                })
                log(f"  patch fail {bid}: {e}")
            sleep_jitter(0.1)

        # apply reference_base 改写：
        # 1) 先删除目标端残留的 reference_base（从后往前避免 index 偏移）
        for child_idx, block_id in sorted(ref_deletes, reverse=True):
            try:
                _delete_block(dst, block_id, child_idx, target_profile)
                log(f"  ref_base deleted: idx={child_idx} {short(block_id,12)}")
            except LarkCliError as e:
                counter["ref_base_fail"] += 1
                counter["patch_errors"].append({"doc": dst, "stage": "ref_base_delete", "error": str(e)})
                log(f"  ref_base delete fail: {e}")
            sleep_jitter(0.1)

        # 2) 插入 mention_doc（从后往前避免 index 偏移）
        for insert_idx, old_token, dst_node in sorted(ref_inserts, reverse=True):
            base_token = old_token.split("_")[0] if "_" in old_token else old_token
            ref_obj_type = m["obj_type"].get(base_token, "bitable")

            try:
                _insert_mention_doc_block(dst, insert_idx, dst_node, ref_obj_type, target_profile)
                counter["ref_base_rewrite"] += 1
                log(f"  ref_base inserted: {short(old_token,12)} → @{short(dst_node,12)} at idx={insert_idx}")
            except LarkCliError as e:
                counter["ref_base_fail"] += 1
                counter["patch_errors"].append({"doc": dst, "stage": "ref_base_insert", "error": str(e)})
                log(f"  ref_base insert fail: {e}")
            sleep_jitter(0.1)

        sleep_jitter(0.2)

    # -----------------------------------------------------------------------
    # Sheet 链接改写
    # -----------------------------------------------------------------------
    sheets = _sheet_targets(m)
    if sheets:
        log(f"sheet targets: {len(sheets)}  (dry_run={dry_run})")

    for idx, (src, dst) in enumerate(sheets, 1):
        try:
            sheet_ids = _list_sheet_ids(dst, target_profile)
        except LarkCliError as e:
            log(f"[sheet {idx}/{len(sheets)}] FAIL list_sheets {dst}: {e}")
            counter["patch_errors"].append({"doc": dst, "stage": "list_sheets", "error": str(e)})
            continue

        doc_changed = False
        for sheet_id, sheet_title in sheet_ids:
            try:
                values = _read_sheet_values(dst, sheet_id, target_profile)
            except LarkCliError as e:
                log(f"  FAIL read sheet {sheet_title}: {e}")
                continue

            counter["sheets_scanned"] += 1
            changed_cells = []  # [(row_idx, col_idx, new_cell)]

            for ri, row in enumerate(values):
                if not row:
                    continue
                for ci, cell in enumerate(row):
                    c, new_cell = _rewrite_sheet_cell(cell, m, target_domain, counter)
                    if c:
                        changed_cells.append((ri, ci, new_cell))

            if not changed_cells:
                continue

            doc_changed = True
            log(f"[sheet {idx}/{len(sheets)}] {short(dst,16)} / {sheet_title} → {len(changed_cells)} cells")

            if dry_run:
                counter["sheets_changed"] += len(changed_cells)
                continue

            for ri, ci, new_cell in changed_cells:
                try:
                    _write_sheet_cell(dst, sheet_id, ri, ci, new_cell, target_profile)
                    counter["sheets_changed"] += 1
                except LarkCliError as e:
                    counter["patch_errors"].append({
                        "doc": dst, "sheet": sheet_id, "row": ri, "col": ci,
                        "stage": "sheet_write", "error": str(e),
                    })
                    log(f"  sheet write fail row={ri} col={ci}: {e}")
                sleep_jitter(0.1)

        if doc_changed:
            per_doc.append({"src": src, "dst": dst, "type": "sheet"})

    # -----------------------------------------------------------------------
    # Bitable 链接改写
    # -----------------------------------------------------------------------
    bitables = _bitable_targets(m)
    if bitables:
        log(f"bitable targets: {len(bitables)}  (dry_run={dry_run})")

    for idx, (src, dst) in enumerate(bitables, 1):
        try:
            tables = _list_bitable_tables(dst, target_profile)
        except LarkCliError as e:
            log(f"[bitable {idx}/{len(bitables)}] FAIL list_tables {dst}: {e}")
            counter["patch_errors"].append({"doc": dst, "stage": "list_bitable_tables", "error": str(e)})
            continue

        counter["bitables_scanned"] += 1
        doc_changed = False

        for table_id, table_name in tables:
            # 找出需要检查的字段：text(1) 和 hyperlink(15)
            try:
                all_fields = _list_bitable_fields(dst, table_id, target_profile)
            except LarkCliError as e:
                log(f"  FAIL list_fields {table_name}: {e}")
                continue

            link_fields = [(fid, fname, ftype) for fid, fname, ftype in all_fields if ftype in (1, 15)]
            if not link_fields:
                continue

            try:
                records = _list_bitable_records(dst, table_id, target_profile)
            except LarkCliError as e:
                log(f"  FAIL list_records {table_name}: {e}")
                continue

            changed_count = 0
            for rec in records:
                rid = rec.get("record_id")
                if not rid:
                    continue
                changed, update_fields = _rewrite_bitable_record(rec, link_fields, m, target_domain, counter)
                if not changed:
                    continue
                changed_count += 1

                if dry_run:
                    continue

                try:
                    _update_bitable_record(dst, table_id, rid, update_fields, target_profile)
                    counter["bitable_records_changed"] += 1
                except LarkCliError as e:
                    counter["patch_errors"].append({
                        "doc": dst, "table": table_id, "record": rid,
                        "stage": "bitable_update", "error": str(e),
                    })
                    log(f"  bitable update fail {rid}: {e}")
                sleep_jitter(0.1)

            if changed_count:
                doc_changed = True
                log(f"[bitable {idx}/{len(bitables)}] {short(dst,16)} / {table_name} → {changed_count} records")
                if dry_run:
                    counter["bitable_records_changed"] += changed_count

        if doc_changed:
            per_doc.append({"src": src, "dst": dst, "type": "bitable"})

    # 报告输出
    out_path = os.path.join(STATE_DIR, "mentions-report.json")
    payload = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "summary": {k: v for k, v in counter.items() if k not in ("external_urls", "patch_errors")},
        "external_urls_sample": counter["external_urls"][:200],
        "patch_errors": counter["patch_errors"][:200],
        "per_doc": per_doc[:500],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"summary saved: {out_path}")
    log(f"  blocks_scanned={counter['blocks_scanned']} blocks_changed={counter['blocks_changed']}")
    log(f"  link rewrite={counter['link_rewrite']} external={counter['link_external']}")
    log(f"  mention_doc rewrite={counter['mention_doc_rewrite']} external={counter['mention_doc_external']}")
    log(f"  ref_base rewrite={counter['ref_base_rewrite']} fail={counter['ref_base_fail']}")
    log(f"  sheet scanned={counter['sheets_scanned']} changed={counter['sheets_changed']}")
    log(f"  sheet mention rewrite={counter['sheet_mention_rewrite']} external={counter['sheet_mention_external']}")
    log(f"  sheet url rewrite={counter['sheet_url_rewrite']} external={counter['sheet_url_external']}")
    log(f"  bitable scanned={counter['bitables_scanned']} records_changed={counter['bitable_records_changed']}")
    log(f"  bitable mention rewrite={counter['bitable_mention_rewrite']} external={counter['bitable_mention_external']}")
    log(f"  bitable url rewrite={counter['bitable_url_rewrite']} external={counter['bitable_url_external']}")
    if not dry_run:
        log(f"  patch ok={counter['patch_ok']} fail={counter['patch_fail']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="rewrite cross-tenant mentions/links in copied docs")
    ap.add_argument("--target-profile", default="")
    ap.add_argument("--target-domain", default="")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = ap.parse_args()
    return _run(args, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
