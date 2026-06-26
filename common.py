"""共用工具：lark-cli 调用、state 读写、日志、URL 解析。

跨租户 wiki 迁移：走"添加协作者 + 服务端副本"路径。
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
MAPPING_PATH = os.path.join(STATE_DIR, "mapping.json")
REPORT_HTML_PATH = os.path.join(STATE_DIR, "report.html")

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# lark-cli 调用
# ---------------------------------------------------------------------------

class LarkCliError(RuntimeError):
    def __init__(self, code, msg, raw=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.raw = raw


def _which_lark_cli():
    p = shutil.which("lark-cli")
    if not p:
        raise LarkCliError("no-cli", "lark-cli not on PATH")
    return p


def lark_cli(args, profile, as_user=True, timeout=60, parse_json=True):
    """通用 lark-cli 调用，返回解析后的 JSON dict。

    args: 不带 lark-cli 自身的参数列表，如 ["api", "GET", "/open-apis/..."]
    """
    cli = _which_lark_cli()
    cmd = [cli] + list(args) + ["--profile", profile]
    if as_user:
        # 仅当 args 里没有显式 --as 时才追加
        if "--as" not in args:
            cmd += ["--as", "user"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise LarkCliError("timeout", f"command timed out: {' '.join(cmd)}") from e

    out = proc.stdout.strip()
    if not parse_json:
        if proc.returncode != 0:
            raise LarkCliError("exit", proc.stderr.strip() or out)
        return out

    if not out:
        # lark-cli 错误时 JSON 输出到 stderr
        err = proc.stderr.strip()
        if err:
            try:
                edata = json.loads(err)
                ecode = (edata.get("error") or {}).get("code") or "empty"
                emsg = (edata.get("error") or {}).get("message") or err
                raise LarkCliError(ecode, emsg, raw=edata)
            except (json.JSONDecodeError, ValueError):
                pass
        raise LarkCliError("empty", err or "empty output")

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # +xx 命令偶尔会先打印一行人类可读再吐 JSON；取最后一段
        last = out.rfind("{")
        if last >= 0:
            try:
                data = json.loads(out[last:])
            except json.JSONDecodeError as e:
                raise LarkCliError("json", f"unparsable: {out[:200]}") from e
        else:
            raise LarkCliError("json", f"unparsable: {out[:200]}")

    return data


def api(method, path, params=None, data=None, profile=None, as_user=True, timeout=60):
    """直接调 OpenAPI；返回 {code, data, msg}。code != 0 抛 LarkCliError。"""
    args = ["api", method.upper(), path]
    if params:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    if data is not None:
        args += ["--data", json.dumps(data, ensure_ascii=False)]
    res = lark_cli(args, profile=profile, as_user=as_user, timeout=timeout)
    code = res.get("code")
    if code != 0:
        raise LarkCliError(code, res.get("msg") or res.get("error") or "", raw=res)
    return res.get("data") or {}


def api_paged(method, path, params=None, profile=None, as_user=True, item_key="items", page_size=50, timeout=60):
    """分页拉全部结果。"""
    items = []
    page_token = ""
    base_params = dict(params or {})
    while True:
        p = dict(base_params)
        p["page_size"] = page_size
        if page_token:
            p["page_token"] = page_token
        data = api(method, path, params=p, profile=profile, as_user=as_user, timeout=timeout)
        chunk = data.get(item_key) or []
        items.extend(chunk)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
        time.sleep(0.2)
    return items


# ---------------------------------------------------------------------------
# state / mapping
# ---------------------------------------------------------------------------

def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_mapping(path=MAPPING_PATH):
    if not os.path.exists(path):
        return _empty_mapping()
    with open(path, "r", encoding="utf-8") as f:
        m = json.load(f)
    # 补齐字段
    base = _empty_mapping()
    base.update(m)
    return base


def save_mapping(m, path=MAPPING_PATH):
    m["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write(path, json.dumps(m, ensure_ascii=False, indent=2))


def _empty_mapping():
    return {
        "meta": {
            "source_profile": "",
            "target_profile": "",
            "source_space_id": "",
            "target_space_id": "",
            "source_root_node": "",
            "target_root_node": "",
            "source_domain": "",
            "target_domain": "",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": "",
        },
        "node_mapping": {},   # src_node_token -> dst_node_token
        "obj_mapping": {},    # src_obj_token  -> dst_obj_token
        "obj_type": {},       # obj_token (src/dst 都存) -> obj_type
        "titles": {},         # src_node_token -> title (用于报告/调试)
        "parents": {},        # src_node_token -> src_parent_node_token
        "failures": [],       # [{node_token, title, stage, error}]
        "edit_times": {},       # src_node_token -> obj_edit_time (复制时快照，用于增量检测)
        "dst_remap": {},       # old_dst_token -> new_dst_token (增量re-copy后，供fix-mentions改写死链)
        "comment_id_mapping": {},  # "src_doc/src_cid" -> "dst_cid"  评论幂等
        "stats": {},
    }


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------

# https://xxx.feishu.cn/wiki/<node_token>
# https://xxx.feishu.cn/docx/<obj_token>
# https://xxx.feishu.cn/docs/<obj_token>
# https://xxx.feishu.cn/sheets/<obj_token>
# https://xxx.feishu.cn/base/<obj_token>
# https://xxx.feishu.cn/file/<obj_token>
# https://xxx.feishu.cn/mindnotes/<obj_token>  (变体: minder, mindnote)
_URL_RE = re.compile(
    r"https?://([^/\s]+)/(wiki|docx|docs|sheets|base|file|mindnotes?|minder|slides)/([A-Za-z0-9]+)"
)


def parse_lark_url(url):
    """返回 (domain, kind, token) 或 None。kind ∈ wiki|docx|doc|sheet|bitable|file|mindnote|slides"""
    m = _URL_RE.search(url or "")
    if not m:
        return None
    domain, raw_kind, token = m.group(1), m.group(2), m.group(3)
    kind_map = {
        "wiki": "wiki",
        "docx": "docx",
        "docs": "doc",
        "sheets": "sheet",
        "base": "bitable",
        "file": "file",
        "mindnote": "mindnote",
        "mindnotes": "mindnote",
        "minder": "mindnote",
        "slides": "slides",
    }
    return domain, kind_map.get(raw_kind, raw_kind), token


def make_url(domain, kind, token):
    seg = {
        "wiki": "wiki",
        "docx": "docx",
        "doc": "docs",
        "sheet": "sheets",
        "bitable": "base",
        "file": "file",
        "mindnote": "mindnotes",
        "slides": "slides",
    }.get(kind, kind)
    return f"https://{domain}/{seg}/{token}"


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------

def sleep_jitter(base=0.4):
    time.sleep(base)


def short(s, n=60):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"
