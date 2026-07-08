#!/usr/bin/env python3
"""生成 HTML 迁移报告：state/report.html"""

import html
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    log, load_mapping, STATE_DIR, REPORT_HTML_PATH, make_url, short,
)


def _safe_load(name):
    p = os.path.join(STATE_DIR, name)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _esc(s):
    return html.escape(str(s or ""))


def _link(url):
    if not url or not url.startswith("http"):
        return '<span class="na">—</span>'
    return f'<a href="{_esc(url)}" target="_blank">{_esc(url)}</a>'


def _build_obj_title_map(m, tree):
    out = {}
    for n in (tree.get("nodes") or []):
        ot = n.get("obj_token"); title = n.get("title") or ""
        if ot and title:
            out[ot] = title
    for src_obj, dst_obj in m["obj_mapping"].items():
        if dst_obj not in out and src_obj in out:
            out[dst_obj] = out[src_obj]
    return out


def _build_obj_to_node(m, tree):
    out = {}
    for n in (tree.get("nodes") or []):
        ot = n.get("obj_token") or ""
        nt = n.get("node_token") or ""
        if ot and nt:
            out[ot] = nt
    for src_obj, dst_obj in m["obj_mapping"].items():
        if src_obj in out:
            src_node = out[src_obj]
            dst_node = m["node_mapping"].get(src_node, "")
            if dst_node:
                out[dst_obj] = dst_node
    return out


def _wiki_url(obj, m, obj_to_node, side):
    node = obj_to_node.get(obj, "")
    dom = m["meta"].get("source_domain" if side == "src" else "target_domain") or ""
    if node and dom:
        return make_url(dom, "wiki", node)
    obj_type = m["obj_type"].get(obj, "docx")
    return make_url(dom, obj_type, obj) if dom else ""


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, "Helvetica Neue", "PingFang SC", sans-serif;
  font-size: 14px; line-height: 1.7; color: #222; background: #fff;
  padding: 48px 40px 80px; max-width: 1100px; margin: 0 auto;
}
h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 13px; margin-bottom: 36px; }
.overview {
  font-size: 14px; margin-bottom: 40px; padding: 14px 18px;
  background: #f8f8f8; border-radius: 6px;
}
.overview strong { font-weight: 600; font-size: 17px; }
section { margin-bottom: 44px; }
section h2 {
  font-size: 14px; font-weight: 600; margin-bottom: 6px; color: #222;
}
section .note { color: #666; font-size: 13px; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-weight: 500; color: #999; font-size: 12px;
  padding: 8px 10px; border-bottom: 1px solid #ddd; white-space: nowrap;
}
td {
  padding: 10px 10px; border-bottom: 1px solid #f0f0f0;
  vertical-align: top; word-break: break-all;
}
td.name { font-weight: 500; white-space: nowrap; }
a { color: #2563eb; text-decoration: none; word-break: break-all; }
a:hover { text-decoration: underline; }
.na { color: #ccc; }
.tag {
  display: inline-block; background: #f0f0f0; border-radius: 3px;
  padding: 1px 6px; font-size: 11px; color: #666; white-space: nowrap;
}
.empty-msg { color: #999; font-style: italic; padding: 40px 0; text-align: center; }
"""


def main():
    m = load_mapping()
    tree = _safe_load("source-tree.json") or {}
    mreport = _safe_load("mentions-report.json") or {}
    creport = _safe_load("comments-report.json") or {}

    obj_title = _build_obj_title_map(m, tree)
    obj_to_node = _build_obj_to_node(m, tree)
    dst_to_src = {v: k for k, v in m["obj_mapping"].items()}

    total_nodes = len(tree.get("nodes") or [])
    migrated_nodes = len(m.get("node_mapping") or {})
    copy_failures = m.get("failures") or []
    patch_errors = mreport.get("patch_errors") or []
    cmt_fails = creport.get("failures") or []
    cmt_fbs = creport.get("fallbacks") or []
    cmt_imgs = creport.get("images_skipped") or []
    cmt_unsupported = creport.get("unsupported_comments") or []

    # 回收站提醒
    has_trash = bool(m.get("dst_remap"))
    trash_hint = ""
    if has_trash:
        trash_hint = '''<div style="margin-bottom:40px;padding:14px 18px;background:#fff8e1;border-left:4px solid #ffc107;border-radius:4px;font-size:13px;">
⚠️ 检测到增量同步产生了回收节点。请在目标知识库中找到 <strong>_迁移回收站</strong> 节点，手动删除它（删除此页面和它包含的所有子页面）。
</div>'''

    sections = []

    # 1) 节点复制失败
    if copy_failures:
        rows = ""
        for f in copy_failures:
            title = _esc(f.get("title") or "(无标题)")
            otype = _esc(f.get("obj_type") or "")
            sl = _link(_wiki_url(f.get("obj_token") or "", m, obj_to_node, "src"))
            rows += f'<tr><td class="name">{title}</td><td><span class="tag">{otype}</span></td><td>{sl}</td></tr>\n'
        sections.append(f'''<section>
<h2>节点复制失败（{len(copy_failures)}）</h2>
<p class="note">需要手动从源端导出再上传到目标知识库。</p>
<table>
<tr><th>文档名</th><th>类型</th><th>源链接</th></tr>
{rows}</table>
</section>''')

    # 2) 链接重写失败
    if patch_errors:
        seen = set(); rows = ""
        for e in patch_errors:
            dst = e.get("doc") or ""
            if dst in seen: continue
            seen.add(dst)
            src = e.get("src_doc") or dst_to_src.get(dst) or ""
            title = _esc(obj_title.get(src) or obj_title.get(dst) or "(未知)")
            otype = _esc(m["obj_type"].get(src) or m["obj_type"].get(dst) or "")
            sl = _link(_wiki_url(src, m, obj_to_node, "src"))
            tl = _link(_wiki_url(dst, m, obj_to_node, "dst"))
            rows += f'<tr><td class="name">{title}</td><td><span class="tag">{otype}</span></td><td>{sl}</td><td>{tl}</td></tr>\n'
        sections.append(f'''<section>
<h2>链接重写失败（{len(seen)}）</h2>
<p class="note">目标文档中的 @文档/链接仍指向源端，需要手动修改。</p>
<table>
<tr><th>文档名</th><th>类型</th><th>源链接</th><th>目标链接</th></tr>
{rows}</table>
</section>''')

    # 3) 评论迁移失败
    if cmt_fails:
        seen = set(); rows = ""
        for f in cmt_fails:
            src = f.get("src_doc") or ""; dst = f.get("dst_doc") or ""
            key = dst or src
            if key in seen: continue
            seen.add(key)
            title = _esc(obj_title.get(src) or obj_title.get(dst) or "(未知)")
            otype = _esc(m["obj_type"].get(src) or m["obj_type"].get(dst) or "")
            sl = _link(_wiki_url(src, m, obj_to_node, "src"))
            tl = _link(_wiki_url(dst, m, obj_to_node, "dst"))
            rows += f'<tr><td class="name">{title}</td><td><span class="tag">{otype}</span></td><td>{sl}</td><td>{tl}</td></tr>\n'
        sections.append(f'''<section>
<h2>评论迁移失败（{len(seen)}）</h2>
<p class="note">需要打开源文档查看评论，手动复制到目标文档。</p>
<table>
<tr><th>文档名</th><th>类型</th><th>源链接</th><th>目标链接</th></tr>
{rows}</table>
</section>''')

    # 4) 评论位置不准
    if cmt_fbs:
        seen = set(); rows = ""
        for f in cmt_fbs:
            src = f.get("src_doc") or ""; dst = f.get("dst_doc") or ""
            if dst in seen: continue
            seen.add(dst)
            title = _esc(obj_title.get(src) or obj_title.get(dst) or "(未知)")
            otype = _esc(m["obj_type"].get(src) or m["obj_type"].get(dst) or "")
            sl = _link(_wiki_url(src, m, obj_to_node, "src"))
            tl = _link(_wiki_url(dst, m, obj_to_node, "dst"))
            rows += f'<tr><td class="name">{title}</td><td><span class="tag">{otype}</span></td><td>{sl}</td><td>{tl}</td></tr>\n'
        sections.append(f'''<section>
<h2>评论位置不准（{len(seen)}）</h2>
<p class="note">评论已迁移但变成了全文评论，需要手动移动到正确位置。</p>
<table>
<tr><th>文档名</th><th>类型</th><th>源链接</th><th>目标链接</th></tr>
{rows}</table>
</section>''')

    # 5) 评论图片丢失
    if cmt_imgs:
        rows = ""
        for item in cmt_imgs:
            text = _esc(short(item.get("comment_text") or "", 40))
            n_img = len(item.get("image_tokens") or [])
            sl = _link(item.get("src_url") or "")
            tl = _link(item.get("dst_url") or "")
            rows += f'<tr><td class="name">{text}</td><td>{n_img}</td><td>{sl}</td><td>{tl}</td></tr>\n'
        sections.append(f'''<section>
<h2>评论图片丢失（{len(cmt_imgs)}）</h2>
<p class="note">评论文字已迁移，需要从源文档评论中手动复制图片到目标。</p>
<table>
<tr><th>评论内容</th><th>图片数</th><th>源链接</th><th>目标链接</th></tr>
{rows}</table>
</section>''')

    # 6) 不支持迁移的评论
    if cmt_unsupported:
        rows = ""
        total_replies = 0
        for item in cmt_unsupported:
            src_type = _esc(item.get("src_type") or "")
            n_replies = item.get("reply_count") or 0
            total_replies += n_replies
            title = _esc(obj_title.get(item.get("src_obj")) or "(未知)")
            sl = _link(item.get("src_url") or "")
            tl = _link(item.get("dst_url") or "")
            rows += f'<tr><td class="name">{title}</td><td><span class="tag">{src_type}</span></td><td>{n_replies}</td><td>{sl}</td><td>{tl}</td></tr>\n'
        sections.append(f'''<section>
<h2>不支持迁移的评论（{total_replies}）</h2>
<p class="note">电子表格和多维表格的评论需要手动在目标端重建。</p>
<table>
<tr><th>文档名</th><th>类型</th><th>评论数</th><th>源链接</th><th>目标链接</th></tr>
{rows}</table>
</section>''')

    body = '\n'.join(sections)
    if not body.strip():
        body = '<p class="empty-msg">没有需要人工处理的问题。</p>'

    html_doc = f'''<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>迁移报告</title>
<style>{CSS}</style>
</head>
<body>
<h1>迁移报告</h1>
<p class="subtitle">{_esc(m["meta"].get("source_profile") or "")} → {_esc(m["meta"].get("target_profile") or "")}　·　{datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
<div class="overview">本次应迁 <strong>{total_nodes}</strong> 个节点，实迁 <strong>{migrated_nodes}</strong> 个</div>
{trash_hint}
{body}
</body>
</html>
'''

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REPORT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)
    log(f"report saved: {REPORT_HTML_PATH}")


if __name__ == "__main__":
    main()
