import hashlib
import json
import re
import shutil
import sys
import subprocess
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

"""
archiver.py (app2)
提取自 mw_fetch5.0.py，作为可导入模块使用：
- 使用 archive_model(url, cookie, download_dir, logs_dir, logger=None)
- 不包含全局 URL/COOKIE/OUT_DIR 配置
- 保持 mw_fetch5.0 的采集、curl 兜底、3MF 获取、实例/图片处理逻辑
"""

import requests
from bs4 import BeautifulSoup


def log(*args):
    print("[MW-FETCH]", *args)


def log_section(title: str):
    log("")
    log("=" * 10, title, "=" * 10)


def emit_progress(progress_callback, percent: int, message: str, extra: Optional[dict] = None):
    if not callable(progress_callback):
        return
    payload = {
        "percent": max(min(int(percent or 0), 100), 0),
        "message": str(message or "").strip(),
    }
    if isinstance(extra, dict) and extra:
        payload.update(extra)
    try:
        progress_callback(payload)
    except Exception:
        pass


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def pick_ext_from_url(url: str, fallback: str = "jpg") -> str:
    clean = url.split("#")[0].split("?")[0]
    m = re.search(r"\.([A-Za-z0-9]+)$", clean)
    return m.group(1) if m else fallback


def parse_cookies(cookie_str: str) -> Dict[str, str]:
    cookie_str = cookie_str.strip()
    # 兼容带前缀 "Cookie:" 的整行
    if cookie_str.lower().startswith("cookie:"):
        cookie_str = cookie_str.split(":", 1)[1].strip()
    cookies = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _extract_auth_token(raw_cookie: str) -> str:
    cookies = parse_cookies(raw_cookie or "")
    return (
        cookies.get("token")
        or cookies.get("access_token")
        or cookies.get("accessToken")
        or ""
    )


IMAGE_TRANSFER_TIMEOUT_SECONDS = 45
BINARY_TRANSFER_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 30


def _stage_percent(start: int, end: int, current: int, total: int) -> int:
    if total <= 0:
        return end
    bounded_current = min(max(current, 0), total)
    return start + int((bounded_current / total) * max(end - start, 0))


def _emit_stage_progress(
    progress_callback,
    start: int,
    end: int,
    current: int,
    total: int,
    message: str,
):
    if total <= 0:
        emit_progress(progress_callback, end, message)
        return
    emit_progress(
        progress_callback,
        _stage_percent(start, end, current, total),
        f"{message}（{current}/{total}）",
        {"current": current, "total": total},
    )


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    overwrite: bool = False,
    *,
    timeout: tuple[int, int] = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    max_duration: int = IMAGE_TRANSFER_TIMEOUT_SECONDS,
):
    if dest.exists() and not overwrite:
        log("存在，跳过：", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest.with_name(f"{dest.name}.part")
    started_at = time.monotonic()
    log("开始下载：", url, "->", dest)
    try:
        with session.get(url, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            with temp_dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if max_duration > 0 and time.monotonic() - started_at > max_duration:
                        raise TimeoutError(f"下载超时（>{max_duration}s）: {url}")
                    if not chunk:
                        continue
                    f.write(chunk)
        temp_dest.replace(dest)
    except Exception:
        try:
            if temp_dest.exists():
                temp_dest.unlink()
        except Exception:
            pass
        raise
    log("已下载：", dest)


def pick_instance_filename(inst: dict, name_hint: str = "") -> str:
    base = sanitize_filename(
        inst.get("fileName")
        or inst.get("name")
        or inst.get("sourceFileName")
        or inst.get("localName")
        or inst.get("title")
        or str(inst.get("id") or "model")
    ).strip()
    if not base:
        base = str(inst.get("id") or "model")
    # base 可能已经包含 .3mf，避免拼成 xxx.3mf.3mf
    if base.lower().endswith(".3mf"):
        return base
    return f"{base}.3mf"


def choose_unique_instance_filename(
    inst: dict,
    all_instances: List[dict],
    instances_dir: Path,
    name_hint: str = "",
) -> str:
    """
    为实例选择“不会与其它实例冲突”的 3MF 文件名。

    规则：
    1) 优先使用当前实例已有 fileName（若安全）
    2) 否则使用 pick_instance_filename 结果
    3) 若冲突（其它实例已占用或磁盘已存在）则自动追加 _{id} / _{n}
    """
    explicit_raw = str(inst.get("fileName") or "").strip()
    explicit_name = ""
    if explicit_raw:
        explicit_name = sanitize_filename(Path(explicit_raw).name)
        if explicit_name and not explicit_name.lower().endswith(".3mf"):
            explicit_name += ".3mf"

    preferred = explicit_name or pick_instance_filename(inst, name_hint)
    if not preferred:
        preferred = f"{inst.get('id') or 'model'}.3mf"

    # 收集“其它实例”已声明的 fileName，避免多个实例指向同一文件
    used_by_others = set()
    for other in all_instances or []:
        if other is inst or not isinstance(other, dict):
            continue
        raw = str(other.get("fileName") or "").strip()
        if not raw:
            continue
        nm = sanitize_filename(Path(raw).name)
        if nm and not nm.lower().endswith(".3mf"):
            nm += ".3mf"
        if nm:
            used_by_others.add(nm)

    def _can_use(name: str) -> bool:
        if not name:
            return False
        if name in used_by_others:
            return False
        # 当前实例若已有 fileName，且文件存在，可复用该文件名
        if explicit_name and name == explicit_name:
            return True
        # 否则磁盘存在同名文件时视为冲突，避免覆盖/误复用
        if (instances_dir / name).exists():
            return False
        return True

    if _can_use(preferred):
        return preferred

    stem = Path(preferred).stem or str(inst.get("id") or "model")
    ext = Path(preferred).suffix or ".3mf"

    inst_id = str(inst.get("id") or "").strip()
    if inst_id:
        candidate = sanitize_filename(f"{stem}_{inst_id}{ext}")
        if _can_use(candidate):
            return candidate

    idx = 1
    while True:
        candidate = sanitize_filename(f"{stem}_{idx}{ext}")
        if _can_use(candidate):
            return candidate
        idx += 1


def fetch_html_with_requests(session: requests.Session, url: str, raw_cookie: str) -> Optional[str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if raw_cookie:
        headers["Cookie"] = raw_cookie
    try:
        resp = session.get(url, timeout=30, headers=headers)
    except Exception as e:
        log("requests 获取页面失败:", e)
        return None
    if resp.status_code >= 400:
        log("requests 获取页面状态异常:", resp.status_code)
        return None
    return resp.text


def fetch_html_with_curl(url: str, raw_cookie: str) -> str:
    """
    备用：使用 curl 拉取页面，尽量复刻浏览器最小头。
    """
    cmd = [
        "curl",
        "-sSL",
        "--compressed",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "-H",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-H",
        "Cache-Control: no-cache",
        "-H",
        "Connection: keep-alive",
        "-H",
        f"Cookie: {raw_cookie}",
        "-H",
        "Pragma: no-cache",
        "-H",
        "Upgrade-Insecure-Requests: 1",
        "-H",
        "Sec-Fetch-Dest: document",
        "-H",
        "Sec-Fetch-Mode: navigate",
        "-H",
        "Sec-Fetch-Site: none",
        "-H",
        "Sec-Fetch-User: ?1",
        "-H",
        "User-Agent: Mozilla/5.0 (MW-Fetcher-curl)",
        url,
    ]
    log("尝试 curl 获取页面:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=False)
    if result.returncode != 0:
        err_msg = result.stderr.decode(errors="ignore") if result.stderr else ""
        raise RuntimeError(f"curl 失败 code={result.returncode} stderr={err_msg[:300]}")

    stdout = result.stdout or b""
    log("curl 返回长度:", len(stdout))

    # 尝试直接 utf-8 解码
    try:
        return stdout.decode("utf-8")
    except Exception:
        # 若仍是 gzip/其它编码，尝试解压
        try:
            import gzip
            return gzip.decompress(stdout).decode("utf-8", errors="ignore")
        except Exception:
            return stdout.decode("utf-8", errors="ignore")


def _json_loads_maybe(raw: str) -> Optional[object]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def extract_next_data(html_text: str) -> dict:
    patterns = [
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'__NEXT_DATA__\s*=\s*({.*?})\s*;',
        r'window\.__NEXT_DATA__\s*=\s*({.*?})\s*;',
        r'__NUXT__\s*=\s*({.*?})\s*;',
        r'window\.__NUXT__\s*=\s*({.*?})\s*;',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.S)
        if not m:
            continue
        raw = (m.group(1) or "").strip()
        raw = raw.rstrip(";")
        data = _json_loads_maybe(raw)
        if data is not None:
            return data
    parse_patterns = [
        r'__NEXT_DATA__\s*=\s*JSON\.parse\((\".*?\")\)\s*;',
        r"__NEXT_DATA__\s*=\s*JSON\.parse\(('.*?')\)\s*;",
        r'__NUXT__\s*=\s*JSON\.parse\((\".*?\")\)\s*;',
        r"__NUXT__\s*=\s*JSON\.parse\(('.*?')\)\s*;",
    ]
    for pattern in parse_patterns:
        m = re.search(pattern, html_text, re.S)
        if not m:
            continue
        raw = (m.group(1) or "").strip()
        parsed = _json_loads_maybe(raw)
        if isinstance(parsed, str):
            data = _json_loads_maybe(parsed)
            if data is not None:
                return data
    raise RuntimeError("未找到 __NEXT_DATA__")


def _get_nested(obj: dict, keys: List[str]):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _score_design_candidate(obj: dict) -> int:
    if not isinstance(obj, dict):
        return -1
    score = 0
    if isinstance(obj.get("instances"), list):
        score += 3
    if "designExtension" in obj or "summary" in obj or "summaryHtml" in obj:
        score += 2
    if "tags" in obj or "tagsOriginal" in obj:
        score += 1
    if "coverUrl" in obj or "coverImage" in obj or "thumbnail" in obj or "thumbnailUrl" in obj:
        score += 1
    if "likeCount" in obj or "downloadCount" in obj or "printCount" in obj:
        score += 1
    if "designCreator" in obj or "creatorName" in obj or "author" in obj or "user" in obj:
        score += 1
    if obj.get("id") is not None and obj.get("title"):
        score += 1
    return score


def _find_best_design(obj: object) -> Optional[dict]:
    best = None
    best_score = -1
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "design" in cur and isinstance(cur.get("design"), dict):
                score = _score_design_candidate(cur["design"])
                if score > best_score:
                    best = cur["design"]
                    best_score = score
            score = _score_design_candidate(cur)
            if score > best_score:
                best = cur
                best_score = score
            for val in cur.values():
                stack.append(val)
        elif isinstance(cur, list):
            stack.extend(cur)
    if best_score >= 2:
        return best
    return None


def extract_design_from_next_data(next_data: dict) -> Optional[dict]:
    if not isinstance(next_data, dict):
        return None
    paths = [
        ["props", "pageProps", "design"],
        ["props", "pageProps", "data", "design"],
        ["props", "pageProps", "pageData", "design"],
        ["props", "pageProps", "payload", "design"],
        ["props", "pageProps", "designDetail"],
        ["props", "pageProps", "model"],
        ["props", "pageProps", "detail"],
    ]
    for path in paths:
        candidate = _get_nested(next_data, path)
        if isinstance(candidate, dict):
            if "design" in candidate and isinstance(candidate.get("design"), dict):
                return candidate["design"]
            return candidate
    page_props = _get_nested(next_data, ["props", "pageProps"]) or next_data.get("pageProps")
    return _find_best_design(page_props or next_data)


def _parse_design_id(url: str) -> Optional[int]:
    if not url:
        return None
    m = re.search(r"/models/(\d+)", url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _extract_api_host(html_text: str) -> Optional[str]:
    if not html_text:
        return None
    m = re.search(r'API_HOST"\s*:\s*"([^"]+)"', html_text)
    if not m:
        m = re.search(r"API_HOST'\s*:\s*'([^']+)'", html_text)
    if not m:
        return None
    host = (m.group(1) or "").strip()
    if not host:
        return None
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return f"https://{host}"


def _is_cloudflare_challenge(html_text: str) -> bool:
    if not html_text:
        return False
    lowered = html_text.lower()
    markers = [
        "just a moment",
        "cf_chl",
        "challenge-platform",
        "/cdn-cgi/challenge",
        "cloudflare",
        "attention required",
        "checking your browser",
        "enable javascript and cookies to continue",
    ]
    return any(m in lowered for m in markers)


def _unwrap_design_payload(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return _find_best_design(payload)
    direct = _find_best_design(payload)
    if direct:
        return direct
    for key in ["data", "design", "result", "detail", "model", "info"]:
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            if "design" in candidate and isinstance(candidate.get("design"), dict):
                return candidate["design"]
            picked = _find_best_design(candidate)
            if picked:
                return picked
    return None


def fetch_design_from_api(
    session: requests.Session,
    raw_cookie: str,
    url: str,
    api_host_hint: Optional[str] = None,
) -> Optional[dict]:
    design_id = _parse_design_id(url)
    if not design_id:
        return None
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://makerworld.com.cn"
    base_candidates = []
    if api_host_hint:
        base_candidates.append(api_host_hint)
    base_candidates.append(origin)
    base_candidates.append("https://api.bambulab.cn")
    base_candidates.append("https://api.bambulab.com")
    bases = []
    for base in base_candidates:
        if not base:
            continue
        if base not in bases:
            bases.append(base)

    path_templates = [
        "/api/v1/design-service/design/{id}",
        "/api/v1/design-service/design/{id}/detail",
        "/api/v1/design-service/design/{id}/detail?source=web",
        "/api/v1/design-service/design/{id}?lang=zh",
        "/v1/design-service/design/{id}",
        "/v1/design-service/design/{id}/detail",
    ]
    prefixes = ["", "/makerworld"]
    endpoints = []
    for base in bases:
        for prefix in prefixes:
            for path in path_templates:
                endpoints.append(f"{base.rstrip('/')}{prefix}{path.format(id=design_id)}")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": url,
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if raw_cookie:
        headers["Cookie"] = raw_cookie
    for api_url in endpoints:
        try:
            resp = session.get(api_url, timeout=30, headers=headers)
        except Exception:
            continue
        if resp.status_code >= 400:
            continue
        try:
            payload = resp.json()
        except Exception:
            continue
        design = _unwrap_design_payload(payload)
        if design:
            return design
    return None


def _comment_text_value(value) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        for key in ("text", "content", "value", "raw", "html", "message"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return " ".join(nested.split())
    return ""


def _comment_numeric(value) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except Exception:
        return 0


def _extract_comment_image_candidates(node: dict) -> List[dict]:
    found: List[dict] = []
    if not isinstance(node, dict):
        return found
    keys = (
        "pictures",
        "images",
        "imageList",
        "imageUrls",
        "commentPictures",
        "commentImages",
        "medias",
        "mediaList",
        "photos",
    )
    for key in keys:
        items = node.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            url = ""
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, dict):
                url = str(
                    item.get("url")
                    or item.get("imageUrl")
                    or item.get("src")
                    or item.get("originalUrl")
                    or item.get("downloadUrl")
                    or ""
                ).strip()
            if not url:
                continue
            found.append({"url": url})
    return found


def _normalize_comment_candidate(node: dict) -> Optional[dict]:
    if not isinstance(node, dict):
        return None

    comment_markers = (
        "commentId",
        "rootCommentId",
        "replyCount",
        "subCommentCount",
        "commentTime",
        "commentType",
        "isTop",
        "isPinned",
        "praiseCount",
        "likeCount",
        "rating",
        "score",
        "star",
        "starLevel",
    )
    if not any(key in node for key in comment_markers):
        return None
    if any(key in node for key in ("designExtension", "coverUrl", "downloadCount", "printCount", "instances")):
        return None

    content = ""
    for key in ("commentContent", "content", "comment", "message", "text", "body", "description"):
        content = _comment_text_value(node.get(key))
        if content:
            break
    if not content:
        return None

    user = node.get("user") or node.get("author") or node.get("creator") or node.get("commentUser") or {}
    if not isinstance(user, dict):
        user = {}
    author_name = (
        str(
            user.get("nickname")
            or user.get("nickName")
            or user.get("name")
            or user.get("username")
            or user.get("userName")
            or node.get("nickname")
            or node.get("nickName")
            or node.get("userName")
            or node.get("username")
            or node.get("authorName")
            or node.get("creatorName")
            or ""
        ).strip()
    )
    author_avatar = str(
        user.get("avatarUrl")
        or user.get("avatar")
        or user.get("headImg")
        or node.get("avatarUrl")
        or node.get("avatar")
        or ""
    ).strip()
    author_url = str(user.get("homepage") or user.get("url") or node.get("authorUrl") or "").strip()

    comment_id = str(node.get("commentId") or node.get("id") or node.get("rootCommentId") or "").strip()
    created_at = str(
        node.get("commentTime")
        or node.get("createTime")
        or node.get("createdAt")
        or node.get("publishTime")
        or node.get("time")
        or ""
    ).strip()
    like_count = _comment_numeric(node.get("likeCount") or node.get("praiseCount"))
    reply_count = _comment_numeric(node.get("replyCount") or node.get("subCommentCount") or node.get("childrenCount"))
    rating = _comment_numeric(node.get("rating") or node.get("score") or node.get("star") or node.get("starLevel"))
    if rating < 0:
        rating = 0
    if rating > 5:
        rating = 5

    badges: List[str] = []
    if node.get("isTop") or node.get("isPinned"):
        badges.append("置顶")
    if node.get("isBoost") or node.get("isBoosted"):
        badges.append("已助力")
    if node.get("designerReplied") or node.get("hasDesignerReply") or node.get("isOfficialReply"):
        badges.append("设计师已回复")
    profile_name = str(node.get("profileName") or node.get("profileTitle") or "").strip()
    if profile_name:
        badges.append(profile_name)

    images = _extract_comment_image_candidates(node)
    stable_id = comment_id or hashlib.sha1(
        f"{author_name}|{created_at}|{content}".encode("utf-8", errors="ignore")
    ).hexdigest()[:16]

    return {
        "id": stable_id,
        "author": {
            "name": author_name,
            "avatarUrl": author_avatar,
            "avatarLocal": "",
            "avatarRelPath": "",
            "url": author_url,
        },
        "content": content,
        "createdAt": created_at,
        "likeCount": like_count,
        "replyCount": reply_count,
        "rating": rating,
        "badges": badges,
        "images": images,
    }


def _collect_comments_from_payload(node: object, out: List[dict], seen: set[str], depth: int = 0):
    if depth > 12 or node is None:
        return
    if isinstance(node, list):
        for item in node:
            _collect_comments_from_payload(item, out, seen, depth + 1)
        return
    if not isinstance(node, dict):
        return

    comment = _normalize_comment_candidate(node)
    if comment:
        comment_id = str(comment.get("id") or "").strip()
        if comment_id and comment_id not in seen:
            seen.add(comment_id)
            out.append(comment)

    for value in node.values():
        _collect_comments_from_payload(value, out, seen, depth + 1)


def _extract_comment_count_from_payload(node: object, depth: int = 0, found: Optional[List[int]] = None) -> List[int]:
    if found is None:
        found = []
    if depth > 10 or node is None:
        return found
    if isinstance(node, list):
        for item in node:
            _extract_comment_count_from_payload(item, depth + 1, found)
        return found
    if not isinstance(node, dict):
        return found
    for key, value in node.items():
        lowered = str(key or "").strip().lower()
        if lowered in {"commentcount", "commentscount", "reviewcount", "commenttotal", "totalcomments"}:
            numeric = _comment_numeric(value)
            if 0 <= numeric <= 50000:
                found.append(numeric)
        _extract_comment_count_from_payload(value, depth + 1, found)
    return found


def collect_comments(
    next_data: dict,
    design: dict,
    session: requests.Session,
    out_dir: Path,
    progress_callback=None,
    progress_start: int = 50,
    progress_end: int = 55,
) -> dict:
    emit_progress(progress_callback, progress_start, "正在整理评论数据")
    comments: List[dict] = []
    seen: set[str] = set()
    _collect_comments_from_payload(next_data, comments, seen)
    _collect_comments_from_payload(design, comments, seen)

    comment_count = 0
    hints = _extract_comment_count_from_payload(next_data)
    if hints:
        comment_count = max(hints)
    if comment_count <= 0:
        counts = design.get("counts") or {}
        comment_count = (
            _comment_numeric(design.get("commentCount"))
            or _comment_numeric(design.get("commentsCount"))
            or _comment_numeric(design.get("reviewCount"))
            or _comment_numeric(counts.get("comments"))
        )

    asset_total = 0
    for item in comments:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        if str(author.get("avatarUrl") or "").strip():
            asset_total += 1
        images = item.get("images") if isinstance(item.get("images"), list) else []
        asset_total += sum(1 for image in images if isinstance(image, dict) and str(image.get("url") or "").strip())

    asset_index = 0
    for idx, item in enumerate(comments, start=1):
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or "").strip()
        if avatar_url:
            avatar_name = f"comment_{idx:02d}_avatar.{pick_ext_from_url(avatar_url)}"
            asset_index += 1
            if asset_total and (asset_index == 1 or asset_index == asset_total or asset_index % 5 == 0):
                _emit_stage_progress(
                    progress_callback,
                    progress_start,
                    progress_end,
                    asset_index,
                    asset_total,
                    "正在下载评论资源",
                )
            try:
                download_file(
                    session,
                    avatar_url,
                    out_dir / avatar_name,
                    max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                )
                author["avatarLocal"] = avatar_name
                author["avatarRelPath"] = f"images/{avatar_name}"
            except Exception as exc:
                log("评论头像下载失败，保留原始链接：", avatar_url, exc)
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for img_idx, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            url = str(image.get("url") or "").strip()
            if not url:
                continue
            image_name = f"comment_{idx:02d}_img_{img_idx:02d}.{pick_ext_from_url(url)}"
            asset_index += 1
            if asset_total and (asset_index == 1 or asset_index == asset_total or asset_index % 5 == 0):
                _emit_stage_progress(
                    progress_callback,
                    progress_start,
                    progress_end,
                    asset_index,
                    asset_total,
                    "正在下载评论资源",
                )
            try:
                download_file(
                    session,
                    url,
                    out_dir / image_name,
                    max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                )
                image["localName"] = image_name
                image["relPath"] = f"images/{image_name}"
            except Exception as exc:
                log("评论图片下载失败，保留原始链接：", url, exc)

    emit_progress(progress_callback, progress_end, "评论整理完成")

    return {
        "count": max(comment_count, len(comments)),
        "items": comments,
    }


def parse_summary(
    design: dict,
    base_name: str,
    session: requests.Session,
    out_dir: Path,
    progress_callback=None,
    progress_start: int = 40,
    progress_end: int = 45,
):
    raw_html = (
        design.get("summary")
        or design.get("summaryHtml")
        or design.get("summary_html")
        or design.get("summaryContent")
        or design.get("description")
        or design.get("desc")
        or ""
    )
    if isinstance(raw_html, dict):
        raw_html = raw_html.get("html") or raw_html.get("raw") or raw_html.get("text") or ""
    soup = BeautifulSoup(raw_html, "html.parser")
    summary_images = []
    image_nodes = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src:
            image_nodes.append((img, src))

    total_images = len(image_nodes)
    for idx, (img, src) in enumerate(image_nodes, start=1):
        _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            idx,
            total_images,
            "正在下载摘要图片",
        )
        ext = pick_ext_from_url(src)
        name = f"summary_img_{idx:02d}.{ext}"
        try:
            download_file(
                session,
                src,
                out_dir / name,
                max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log("摘要图片下载失败，保留原始链接：", src, exc)
            continue
        img["src"] = f"./images/{name}"
        summary_images.append(
            {
                "index": idx,
                "originalUrl": src,
                "relPath": f"images/{name}",
                "fileName": name,
            }
        )

    if total_images:
        emit_progress(progress_callback, progress_end, "摘要图片整理完成")

    html_local = str(soup)
    text_plain = " ".join(soup.get_text().split())

    return {
        "raw": raw_html,
        "html": html_local,
        "text": text_plain,
        "summaryImages": summary_images,
    }


def extract_author(design: dict, html_text: str = None):
    def _extract_author_handle(href_or_url: str) -> str:
        if not href_or_url:
            return ""
        raw = href_or_url.strip()
        if raw.startswith("@"):
            raw = f"/zh/{raw}"
        parsed = urlparse(raw)
        path = parsed.path or raw
        m = re.search(r"(?:^|/)@([A-Za-z0-9_.-]+)(?:[/?#]|$)", path)
        return m.group(1) if m else ""

    def _build_author_url(handle: str) -> str:
        handle = (handle or "").lstrip("@").strip()
        return f"https://makerworld.com.cn/zh/@{handle}" if handle else ""

    creator = design.get("designCreator") or {}
    name = creator.get("name") or design.get("creatorName") or ""
    username = creator.get("username") or creator.get("handle") or design.get("creatorUsername") or ""
    url = ""
    avatar_url = ""
    cand = design.get("user") or design.get("author") or design.get("designCreator") or design.get("creator") or {}
    if isinstance(cand, dict):
        name = cand.get("nickname") or cand.get("name") or cand.get("username") or name
        username = cand.get("username") or cand.get("userName") or cand.get("slug") or cand.get("handle") or username
        url = cand.get("homepage") or cand.get("url") or ""
        avatar_url = cand.get("avatarUrl") or cand.get("avatar") or cand.get("headImg") or ""
    elif isinstance(cand, str) and not name:
        name = cand
    # 兜底从 design 层获取用户名
    if not username:
        username = design.get("creatorName") or design.get("creatorUsername") or username
    if url:
        handle_from_url = _extract_author_handle(url)
        if handle_from_url:
            # 统一收敛到作者主页地址，避免 /upload、/browsing-history 等路径污染
            url = _build_author_url(handle_from_url)
            if not username:
                username = handle_from_url

    # HTML 兜底：优先从作者链接提取 @userid
    # 若 design 中 url 被污染为 /browsing-history，也强制用 HTML 纠正。
    suspect_url = "browsing-history" in (url or "")
    if (not url or not avatar_url or not name or suspect_url) and html_text:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            link_candidates = list(soup.select("a.user_link[href]")) or list(
                soup.find_all("a", href=re.compile(r"/(?:zh/)?@"))
            )
            for link in link_candidates:
                href = link.get("href") or ""
                handle = _extract_author_handle(href)
                if not handle:
                    continue
                # 命中作者 id 后，始终使用标准主页地址
                if not url or suspect_url:
                    url = _build_author_url(handle)
                if not username:
                    username = handle
                if not name:
                    name = (link.get_text() or "").strip()
                if not avatar_url:
                    img = link.find("img")
                    if img and img.get("src"):
                        avatar_url = img.get("src")
                break
        except Exception as e:
            log("解析作者 DOM 失败:", e)

    # 最终统一为 https://makerworld.com.cn/zh/@{userid}
    final_handle = _extract_author_handle(url) or username
    if final_handle:
        url = _build_author_url(final_handle)
    avatar_local = f"author_avatar.{pick_ext_from_url(avatar_url)}" if avatar_url else ""
    return {
        "name": name,
        "url": url,
        "avatarUrl": avatar_url,
        "avatarLocal": avatar_local,
    }


def _normalize_design_pictures(design: dict) -> List[dict]:
    pics = design.get("designExtension", {}).get("design_pictures")
    if isinstance(pics, list) and pics:
        return pics
    for key in ["designPictures", "design_pictures", "designImages", "images", "pictures"]:
        cand = design.get(key)
        if isinstance(cand, list) and cand:
            return cand
    cover_url = design.get("coverUrl") or design.get("coverImage") or design.get("thumbnail") or design.get("thumbnailUrl")
    if cover_url:
        return [{"url": cover_url}]
    return []


def _attachment_name_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url or "")
    raw_name = Path(parsed.path or "").name
    safe_name = sanitize_filename(raw_name)
    if safe_name:
        return safe_name
    return sanitize_filename(fallback) or fallback


def _dedupe_attachment_local_name(preferred: str, used: set[str]) -> str:
    candidate = sanitize_filename(preferred or "attachment") or "attachment"
    stem = Path(candidate).stem or "attachment"
    suffix = Path(candidate).suffix
    if candidate not in used:
        used.add(candidate)
        return candidate
    idx = 1
    while True:
        alt = sanitize_filename(f"{stem}_{idx}{suffix}") or f"attachment_{idx}{suffix}"
        if alt not in used:
            used.add(alt)
            return alt
        idx += 1


def extract_design_attachments(design: dict) -> List[dict]:
    ext = design.get("designExtension") or {}
    if not isinstance(ext, dict):
        return []

    category_map = {
        "design_guide": "guide",
        "design_bom": "bom",
        "design_other": "other",
    }
    attachments = []
    seen_urls = set()
    used_local_names = set()

    for key, category in category_map.items():
        items = ext.get(key)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items, start=1):
            raw_name = ""
            url = ""
            if isinstance(item, dict):
                raw_name = str(item.get("name") or item.get("fileName") or item.get("filename") or item.get("title") or "").strip()
                url = str(item.get("url") or item.get("downloadUrl") or item.get("download_url") or item.get("src") or "").strip()
            elif isinstance(item, str):
                url = item.strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            fallback_name = f"{category}_{idx}.{pick_ext_from_url(url, 'bin')}"
            source_name = sanitize_filename(Path(raw_name).name) if raw_name else ""
            display_name = source_name or _attachment_name_from_url(url, fallback_name)
            local_name = _dedupe_attachment_local_name(display_name, used_local_names)

            attachments.append({
                "category": category,
                "name": display_name,
                "url": url,
                "source": "page",
                "localName": local_name,
                "relPath": f"file/{local_name}",
            })
    return attachments


def collect_design_images(
    design: dict,
    session: requests.Session,
    out_dir: Path,
    base_name: str,
    progress_callback=None,
    progress_start: int = 45,
    progress_end: int = 50,
):
    pics = _normalize_design_pictures(design)
    if not pics:
        return [], None
    design_images = []
    cover_meta = None
    for idx, p in enumerate(pics, start=1):
        _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            idx,
            len(pics),
            "正在下载设计图片",
        )
        url = ""
        if isinstance(p, str):
            url = p
        elif isinstance(p, dict):
            url = p.get("url") or p.get("imageUrl") or p.get("src") or p.get("originalUrl") or ""
        if not url:
            continue
        ext = pick_ext_from_url(url)
        fname = f"design_{idx:02d}.{ext}"
        rel = f"images/{fname}"
        try:
            download_file(
                session,
                url,
                out_dir / fname,
                max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            log("设计图片下载失败，保留原始链接：", url, exc)
            continue
        meta = {
            "index": idx,
            "originalUrl": url,
            "relPath": rel,
            "fileName": fname,
        }
        design_images.append(meta)
        if cover_meta is None:
            cover_meta = meta
    emit_progress(progress_callback, progress_end, "设计图片整理完成")
    return design_images, cover_meta


def _normalize_api_base(base: Optional[str]) -> Optional[str]:
    if not base:
        return None
    base = base.strip()
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"https://{base}"
    return base.rstrip("/")


def _unique_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in seq:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _build_instance_api_candidates(
    inst_id: int,
    api_url: Optional[str],
    origin: Optional[str],
    api_host_hint: Optional[str],
) -> List[str]:
    candidates = []
    if api_url:
        candidates.append(api_url)

    bases = []
    for base in [origin, api_host_hint, "https://api.bambulab.cn", "https://api.bambulab.com"]:
        normalized = _normalize_api_base(base)
        if normalized:
            bases.append(normalized)

    path_templates = [
        "/api/v1/design-service/instance/{id}/f3mf",
        "/v1/design-service/instance/{id}/f3mf",
    ]
    prefixes = ["", "/makerworld"]
    file_types = ["", "3mf"]

    for base in bases:
        for prefix in prefixes:
            for path in path_templates:
                for file_type in file_types:
                    candidates.append(
                        f"{base}{prefix}{path.format(id=inst_id)}?type=download&fileType={file_type}"
                    )

    return _unique_preserve(candidates)


def _extract_instance_download(data: object) -> tuple[str, str]:
    payload = data
    if isinstance(data, dict):
        payload = data.get("data") or data.get("result") or data
    if not isinstance(payload, dict):
        return "", ""
    name = (
        payload.get("name")
        or payload.get("fileName")
        or payload.get("filename")
        or payload.get("file_name")
        or ""
    )
    url = (
        payload.get("url")
        or payload.get("downloadUrl")
        or payload.get("download_url")
        or payload.get("downloadURL")
        or ""
    )
    return name or "", url or ""


def _normalize_url_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


def _looks_like_instance_api_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    return "/f3mf" in raw or ("design-service/instance/" in raw and "download" in raw)


def _looks_like_3mf_file_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    markers = (
        ".3mf",
        "filetype=3mf",
        "/f3mf/download",
        "content-disposition=",
        "application%2fvnd.ms-package.3dmanufacturing",
        "application/vnd.ms-package.3dmanufacturing",
    )
    return any(marker in raw for marker in markers)


def _instance_identity_keys(inst: object) -> List[str]:
    if not isinstance(inst, dict):
        return []
    keys: List[str] = []
    field_names = (
        "id",
        "profileId",
        "profile_id",
        "profileID",
        "instanceId",
        "instance_id",
        "instanceID",
    )
    for field in field_names:
        value = str(inst.get(field) or "").strip()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    for field in ("fileName", "name", "title"):
        value = sanitize_filename(str(inst.get(field) or "").strip()).lower()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    return keys


def _build_existing_instance_index(instances: object) -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    if not isinstance(instances, list):
        return index
    for item in instances:
        if not isinstance(item, dict):
            continue
        for key in _instance_identity_keys(item):
            index.setdefault(key, item)
    return index


def _find_existing_instance(inst: object, instance_index: Dict[str, dict]) -> dict:
    if not isinstance(inst, dict) or not instance_index:
        return {}
    for key in _instance_identity_keys(inst):
        matched = instance_index.get(key)
        if matched:
            return matched
    return {}


def _extract_instance_download_hint(payload: object) -> tuple[str, str, str]:
    best_name = ""
    best_url = ""
    best_api_url = ""
    best_score = -1
    stack: list[tuple[object, int]] = [(payload, 0)]
    seen: set[int] = set()

    while stack:
        current, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(current, (dict, list)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)

        if isinstance(current, dict):
            name = str(
                current.get("name")
                or current.get("fileName")
                or current.get("filename")
                or current.get("file_name")
                or current.get("title")
                or ""
            ).strip()
            file_type = str(current.get("fileType") or current.get("type") or current.get("ext") or "").strip().lower()
            api_url = _normalize_url_value(
                current.get("apiUrl")
                or current.get("api_url")
                or current.get("downloadApiUrl")
                or current.get("download_api_url")
            )
            explicit_url = _normalize_url_value(
                current.get("downloadUrl")
                or current.get("download_url")
                or current.get("downloadURL")
            )
            loose_url = _normalize_url_value(current.get("url") or current.get("src") or current.get("href"))

            candidate_url = ""
            if explicit_url:
                if _looks_like_instance_api_url(explicit_url):
                    api_url = api_url or explicit_url
                else:
                    candidate_url = explicit_url
            elif loose_url:
                if _looks_like_instance_api_url(loose_url):
                    api_url = api_url or loose_url
                elif _looks_like_3mf_file_url(loose_url) or file_type == "3mf" or name.lower().endswith(".3mf"):
                    candidate_url = loose_url

            score = 0
            if explicit_url and candidate_url:
                score += 60
            elif candidate_url:
                score += 35
            if _looks_like_3mf_file_url(candidate_url):
                score += 30
            if file_type == "3mf":
                score += 20
            if name.lower().endswith(".3mf"):
                score += 20
            score -= depth

            if candidate_url and score > best_score:
                best_name = name
                best_url = candidate_url
                best_api_url = api_url
                best_score = score
            elif api_url and not best_api_url:
                best_api_url = api_url

            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))

    return best_name, best_url, best_api_url


def _looks_like_html(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or "<html" in head


def _stringify_3mf_failure_payload(payload) -> str:
    if payload in ("", None):
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _classify_3mf_fetch_failure(
    *,
    status_code: int = 0,
    text: str = "",
    payload=None,
    cloudflare: bool = False,
) -> dict:
    raw_text = str(text or "").strip()
    payload_text = _stringify_3mf_failure_payload(payload)
    combined = " ".join(part for part in (raw_text, payload_text) if part).lower()

    if cloudflare or _is_cloudflare_challenge(raw_text) or _looks_like_html(raw_text):
        return {
            "state": "cloudflare",
            "message": "下载 3MF 时触发了 Cloudflare 校验，请更新 Cookie 或调整代理。",
        }
    if "每日下载上限" in combined or ("download" in combined and "limit" in combined and "daily" in combined):
        return {
            "state": "download_limited",
            "message": "已达到 MakerWorld 每日下载上限，今日暂停自动重试。",
        }
    if "please log in to download models" in combined or "log in to download models" in combined:
        return {
            "state": "auth_required",
            "message": "下载 3MF 需要有效登录 Cookie，请检查 Cookie 是否过期。",
        }
    if status_code in {401, 403}:
        return {
            "state": "auth_required",
            "message": "下载 3MF 需要有效登录 Cookie，请检查 Cookie 是否过期。",
        }
    if status_code == 404 or "route not found" in combined or "\"detail\":\"not found\"" in combined or "not found" in combined:
        return {
            "state": "not_found",
            "message": "源端没有返回该打印配置的 3MF 下载地址。",
        }
    if status_code >= 400:
        return {
            "state": "http_error",
            "message": f"下载 3MF 失败：上游返回 HTTP {status_code}。",
        }
    return {
        "state": "missing",
        "message": "未获取到 3MF 下载地址。",
    }


def fetch_instance_3mf(
    session: requests.Session,
    inst_id: int,
    raw_cookie: str,
    api_url: str = None,
    api_host_hint: Optional[str] = None,
    origin: Optional[str] = None,
):
    """
    获取实例的 3MF 下载地址，允许外部传入 api_url，并自动回退不同 API Host。
    返回: (name, url, used_api_url, failure_info)
    """
    candidates = _build_instance_api_candidates(inst_id, api_url, origin, api_host_hint)
    auth_token = _extract_auth_token(raw_cookie)
    last_error = None
    last_failure = {"state": "missing", "message": "未获取到 3MF 下载地址。"}
    for candidate in candidates:
        try:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": origin or "https://makerworld.com.cn/",
                "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
            }
            if raw_cookie:
                headers["Cookie"] = raw_cookie
            if auth_token:
                headers["Authorization"] = f"Bearer {auth_token}"
                headers["token"] = auth_token
                headers["X-Token"] = auth_token
                headers["X-Access-Token"] = auth_token
            r = session.get(
                candidate,
                timeout=30,
                headers=headers,
            )
            log("[3MF] GET", candidate, "status", r.status_code)
            text_preview = r.text[:200] if r.text else ""
            log("[3MF] 响应前 200 字符:", text_preview)
            if r.status_code >= 400:
                if _is_cloudflare_challenge(text_preview) or _looks_like_html(text_preview):
                    last_failure = _classify_3mf_fetch_failure(
                        status_code=r.status_code,
                        text=text_preview,
                        cloudflare=True,
                    )
                    continue
                last_error = RuntimeError(f"status={r.status_code}")
                last_failure = _classify_3mf_fetch_failure(status_code=r.status_code, text=text_preview)
                continue
            try:
                data = r.json()
            except Exception as je:
                if _is_cloudflare_challenge(text_preview) or _looks_like_html(text_preview):
                    last_failure = _classify_3mf_fetch_failure(
                        status_code=r.status_code,
                        text=text_preview,
                        cloudflare=True,
                    )
                    continue
                last_error = je
                last_failure = _classify_3mf_fetch_failure(status_code=r.status_code, text=text_preview)
                continue
            name, url = _extract_instance_download(data)
            if url:
                return name, url, candidate, {"state": "available", "message": ""}
            last_failure = _classify_3mf_fetch_failure(status_code=r.status_code, text=text_preview, payload=data)
        except Exception as e:
            last_error = e
            continue

    log("3MF 获取失败(尝试 curl)", inst_id, last_error)
    for candidate in candidates:
        cmd = [
            "curl",
            "-sSL",
            "--compressed",
            "-H",
            "Accept: application/json, text/plain, */*",
            "-H",
            f"Cookie: {raw_cookie}",
            "-H",
            f"Referer: {origin or 'https://makerworld.com.cn/'}",
            "-H",
            f"User-Agent: {session.headers.get('User-Agent', 'Mozilla/5.0 (MW-Fetcher-curl)')}",
        ]
        if auth_token:
            cmd.extend([
                "-H",
                f"Authorization: Bearer {auth_token}",
                "-H",
                f"token: {auth_token}",
                "-H",
                f"X-Token: {auth_token}",
                "-H",
                f"X-Access-Token: {auth_token}",
            ])
        cmd.append(candidate)
        try:
            res = subprocess.run(cmd, capture_output=True, text=False)
            if res.returncode != 0:
                err_msg = res.stderr.decode(errors="ignore") if res.stderr else ""
                log("3MF curl 失败 code=", res.returncode, "stderr:", err_msg[:200])
                last_failure = _classify_3mf_fetch_failure(text=err_msg)
                continue
            body = res.stdout or b""
            preview = body[:200]
            log("3MF curl 返回长度:", len(body), "前 200 字符:", preview)
            preview_text = body.decode("utf-8", errors="ignore")
            try:
                data = json.loads(preview_text)
            except Exception as je:
                log("3MF curl JSON 解析失败:", je)
                last_failure = _classify_3mf_fetch_failure(
                    text=preview_text[:400],
                    cloudflare=_is_cloudflare_challenge(preview_text) or _looks_like_html(preview_text),
                )
                continue
            name, url = _extract_instance_download(data)
            if url:
                return name, url, candidate, {"state": "available", "message": ""}
            last_failure = _classify_3mf_fetch_failure(text=preview_text[:400], payload=data)
        except Exception as ce:
            log("3MF curl 调用异常:", ce)
            last_failure = _classify_3mf_fetch_failure(text=str(ce))
            continue
    return "", "", api_url or "", last_failure


def _match_existing_media_item(items: object, *, url: str = "", index: object = None, url_fields: tuple[str, ...] = ("url",)) -> dict:
    if not isinstance(items, list):
        return {}
    normalized_url = _normalize_url_value(url)
    normalized_index = str(index if index is not None else "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in url_fields:
            candidate = _normalize_url_value(item.get(field))
            if normalized_url and candidate and candidate == normalized_url:
                return item
        if normalized_index and str(item.get("index") if item.get("index") is not None else "").strip() == normalized_index:
            return item
    return {}


def collect_instance_media(
    inst: dict,
    session: requests.Session,
    out_dir: Path,
    base_name: str,
    *,
    download_assets: bool = True,
    existing_instance: Optional[dict] = None,
):
    model_info = (
        inst.get("extention", {}).get("modelInfo")
        or inst.get("extension", {}).get("modelInfo")
        or inst.get("modelInfo")
        or {}
    )
    plates = model_info.get("plates") or model_info.get("plateList") or []
    aux_pics = model_info.get("auxiliaryPictures") or model_info.get("pictures") or inst.get("pictures") or inst.get("auxiliaryPictures") or []
    existing_plates = existing_instance.get("plates") if isinstance(existing_instance, dict) else []
    existing_pics = existing_instance.get("pictures") if isinstance(existing_instance, dict) else []
    plate_out = []
    pics_out = []
    # plates thumbs
    for p in plates:
        thumb = p.get("thumbnail", {}).get("url") or p.get("thumbnailUrl") or p.get("url")
        if not thumb:
            continue
        plate_index = int(p.get("index", 0))
        existing_plate = _match_existing_media_item(
            existing_plates,
            url=thumb,
            index=plate_index,
            url_fields=("thumbnailUrl", "url"),
        )
        record = {
            "index": p.get("index", 0),
            "prediction": p.get("prediction"),
            "weight": p.get("weight"),
            "filaments": p.get("filaments") or [],
            "thumbnailUrl": thumb,
        }
        if str(existing_plate.get("thumbnailRelPath") or "").strip():
            record["thumbnailRelPath"] = str(existing_plate.get("thumbnailRelPath") or "").strip()
        if str(existing_plate.get("thumbnailFile") or "").strip():
            record["thumbnailFile"] = str(existing_plate.get("thumbnailFile") or "").strip()
        if download_assets:
            ext = pick_ext_from_url(thumb)
            fname = sanitize_filename(
                str(existing_plate.get("thumbnailFile") or f"{base_name}_inst{inst.get('id')}_plate_{plate_index:02d}.{ext}")
            ) or f"{base_name}_inst{inst.get('id')}_plate_{plate_index:02d}.{ext}"
            try:
                download_file(
                    session,
                    thumb,
                    out_dir / fname,
                    max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                )
                record["thumbnailRelPath"] = f"images/{fname}"
                record["thumbnailFile"] = fname
            except Exception as exc:
                log("实例分盘缩略图下载失败，保留原始链接：", thumb, exc)
        plate_out.append(record)
    # auxiliary pictures
    pic_idx = 1
    for pic in aux_pics:
        url = ""
        is_real = 0
        if isinstance(pic, str):
            url = pic
        elif isinstance(pic, dict):
            url = pic.get("url") or pic.get("imageUrl") or pic.get("src") or ""
            is_real = pic.get("isRealLifePhoto", 0)
        if not url:
            continue
        existing_pic = _match_existing_media_item(
            existing_pics,
            url=url,
            index=pic_idx,
            url_fields=("url", "originalUrl"),
        )
        record = {
            "index": pic_idx,
            "url": url,
            "isRealLifePhoto": is_real,
        }
        if str(existing_pic.get("relPath") or "").strip():
            record["relPath"] = str(existing_pic.get("relPath") or "").strip()
        if str(existing_pic.get("fileName") or "").strip():
            record["fileName"] = str(existing_pic.get("fileName") or "").strip()
        if download_assets:
            ext = pick_ext_from_url(url)
            fname = sanitize_filename(
                str(existing_pic.get("fileName") or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}")
            ) or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}"
            try:
                download_file(
                    session,
                    url,
                    out_dir / fname,
                    max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                )
                record["relPath"] = f"images/{fname}"
                record["fileName"] = fname
            except Exception as exc:
                log("实例图片下载失败，保留原始链接：", url, exc)
        pics_out.append(record)
        pic_idx += 1
    if not pics_out:
        cover = inst.get("cover") or inst.get("coverUrl")
        if cover:
            existing_pic = _match_existing_media_item(
                existing_pics,
                url=cover,
                index=pic_idx,
                url_fields=("url", "originalUrl"),
            )
            record = {
                "index": pic_idx,
                "url": cover,
                "isRealLifePhoto": 0,
            }
            if str(existing_pic.get("relPath") or "").strip():
                record["relPath"] = str(existing_pic.get("relPath") or "").strip()
            if str(existing_pic.get("fileName") or "").strip():
                record["fileName"] = str(existing_pic.get("fileName") or "").strip()
            if download_assets:
                ext = pick_ext_from_url(cover)
                fname = sanitize_filename(
                    str(existing_pic.get("fileName") or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}")
                ) or f"{base_name}_inst{inst.get('id')}_pic_{pic_idx:02d}.{ext}"
                try:
                    download_file(
                        session,
                        cover,
                        out_dir / fname,
                        max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                    )
                    record["relPath"] = f"images/{fname}"
                    record["fileName"] = fname
                except Exception as exc:
                    log("实例封面下载失败，保留原始链接：", cover, exc)
            pics_out.append(record)
    return plate_out, pics_out


def extract_instances(design: dict) -> List[dict]:
    for key in ["instances", "instanceList", "modelInstances", "profiles", "printProfiles", "printingProfiles"]:
        cand = design.get(key)
        if isinstance(cand, list) and cand:
            return cand
    return []


def build_meta(
    design: dict,
    summary: dict,
    design_images: List[dict],
    cover_meta: Optional[dict],
    instances: List[dict],
    author: dict,
    base_name: str,
    attachments: Optional[List[dict]] = None,
    comments_bundle: Optional[dict] = None,
):
    collect_ts = int(datetime.now().timestamp())
    update_time = datetime.now().isoformat()
    counts = design.get("counts") or {}
    comments_bundle = comments_bundle if isinstance(comments_bundle, dict) else {}
    comment_items = comments_bundle.get("items") if isinstance(comments_bundle.get("items"), list) else []
    comment_count = _comment_numeric(comments_bundle.get("count"))
    stats = {
        "likes": design.get("likeCount") or counts.get("likes") or 0,
        "favorites": design.get("collectionCount") or design.get("favoriteCount") or design.get("favCount") or counts.get("favorites") or 0,
        "downloads": design.get("downloadCount") or counts.get("downloads") or 0,
        "prints": design.get("printCount") or counts.get("prints") or 0,
        "views": design.get("readCount") or counts.get("views") or 0,
        "comments": comment_count or len(comment_items),
    }
    images_design_list = [d["fileName"] for d in design_images]
    summary_image_list = [i["fileName"] for i in summary.get("summaryImages", [])]
    cover_local = cover_meta["fileName"] if cover_meta else ""
    cover_url = (
        design.get("coverUrl")
        or design.get("coverImage")
        or design.get("thumbnail")
        or design.get("thumbnailUrl")
        or (cover_meta.get("originalUrl") if cover_meta else "")
    )
    author_avatar_local = author.get("avatarLocal") or ""
    author_rel = f"images/{author_avatar_local}" if author_avatar_local else ""
    source_url = str(design.get("url") or "").strip().lower()
    if "makerworld.com/" in source_url and "makerworld.com.cn" not in source_url:
        source_value = "mw_global"
    else:
        source_value = "mw_cn"

    return {
        "baseName": base_name,
        "source": source_value,
        "url": design.get("url") or "",
        "id": design.get("id"),
        "slug": design.get("slug") or "",
        "title": design.get("title") or "",
        "titleTranslated": design.get("titleTranslated") or "",
        "coverUrl": cover_url,
        "tags": design.get("tags") or [],
        "tagsOriginal": design.get("tagsOriginal") or [],
        "stats": stats,
        "cover": {
            "url": cover_url if cover_meta is None else cover_meta["originalUrl"],
            "localName": cover_local,
            "relPath": cover_meta["relPath"] if cover_meta else "",
        },
        "author": {
            "name": author.get("name") or "",
            "url": author.get("url") or "",
            "avatarUrl": author.get("avatarUrl") or "",
            "avatarLocal": author_avatar_local,
            "avatarRelPath": author_rel,
        },
        "images": {
            "cover": cover_local,
            "design": images_design_list,
            "summary": summary_image_list,
        },
        "designImages": design_images,
        "summaryImages": summary.get("summaryImages", []),
        "summary": {
            "raw": summary.get("raw", ""),
            "html": summary.get("html", ""),
            "text": summary.get("text", ""),
        },
        "commentCount": comment_count or len(comment_items),
        "comments": comment_items,
        "instances": instances,
        "attachments": attachments or [],
        "collectDate": collect_ts,
        "offlineFiles": {
            "attachments": [str(item.get("localName") or "") for item in (attachments or []) if str(item.get("localName") or "").strip()],
            "printed": [],
        },
        "update_time": update_time,
        "generatedAt": Path().absolute().as_posix(),
        "note": "本文件包含结构化数据与打印配置详情。",
    }


# ============ 本地归档与页面生成（集成 5.0.py 逻辑） ============
REBUILD_SESSION = requests.Session()
REBUILD_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (MW-Fetcher-Rebuild)"
})


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def possible_prefixes(base_name: str):
    prefixes = {base_name}
    if base_name.endswith("_"):
        prefixes.add(base_name.rstrip("_"))
    else:
        prefixes.add(base_name + "_")
    return prefixes


def iter_patterns(root: Path, base_name: str, middles):
    for prefix in possible_prefixes(base_name):
        for mid in middles:
            yield from root.glob(prefix + mid)


def glob_with_prefix_or_plain(root: Path, base_name: str, middles):
    seen = set()
    # 先匹配无前缀
    for mid in middles:
        for p in root.glob(mid):
            if p in seen:
                continue
            seen.add(p)
            yield p
    # 再匹配带前缀
    for p in iter_patterns(root, base_name, middles):
        if p in seen:
            continue
        seen.add(p)
        yield p


def strip_prefix(name: str, base_name: str) -> str:
    for prefix in sorted(possible_prefixes(base_name), key=len, reverse=True):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def move_or_replace(src: Path, dst: Path, label: str):
    if not src.exists():
        return False
    try:
        if src.resolve() == dst.resolve():
            return True
    except Exception:
        pass
    ensure_dir(dst.parent)
    if dst.exists():
        if dst.is_dir():
            log("跳过移动，目标是目录:", src, "->", dst)
            return False
        log(f"覆盖 {label}:", src, "->", dst)
        dst.unlink()
    else:
        log(f"移动 {label}:", src, "->", dst)
    shutil.move(str(src), str(dst))
    return True


def choose_archive_base_name(design_id: int, title: str, existing_root: Optional[Path] = None) -> tuple[str, str]:
    desired = f"MW_{design_id}_{sanitize_filename(title or 'model')}"
    if existing_root is None:
        return desired, "created"
    try:
        root = existing_root.resolve()
    except Exception:
        root = existing_root

    exact_dir = root / desired
    if exact_dir.exists() and exact_dir.is_dir():
        return desired, "updated"

    candidates = [p for p in root.glob(f"MW_{design_id}_*") if p.is_dir()]
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0].name, "updated"
    return desired, "created"


def load_existing_meta(work_dir: Path) -> dict:
    meta_path = work_dir / "meta.json"
    if not meta_path.exists() or not meta_path.is_file():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


STYLE_CSS = """
body {
  font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  margin: 0;
  padding: 0;
  background: #f5f5f5;
  color: #222;
}

.container {
  max-width: 980px;
  margin: 24px auto 40px;
  padding: 24px;
  background: #ffffff;
  box-shadow: 0 0 12px rgba(0,0,0,0.06);
  border-radius: 10px;
}

h1.title {
  font-size: 26px;
  margin: 0 0 8px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.title a.origin-link {
  font-size: 14px;
  text-decoration: none;
  color: #1976d2;
}

.title a.origin-link::before {
  content: "↗ ";
}

.author {
  margin: 4px 0 14px;
  font-size: 14px;
  color: #555;
  display: flex;
  align-items: center;
  gap: 10px;
}

.author img.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  object-fit: cover;
}

.hero {
  width: 100%;
  max-height: 540px;
  object-fit: contain;
  border-radius: 8px;
  margin-bottom: 12px;
  background: #000;
}

.collect-date {
  font-size: 13px;
  color: #777;
  margin: 0 0 16px;
}

.section-title {
  font-size: 18px;
  margin: 22px 0 10px;
  border-left: 4px solid #1976d2;
  padding-left: 10px;
}

.stats {
  margin: 6px 0 14px;
  color: #666;
  font-size: 14px;
}

.tag-list span {
  display: inline-block;
  background: #e3f2fd;
  padding: 4px 10px;
  margin: 4px 6px 0 0;
  border-radius: 14px;
  font-size: 13px;
}

.summary img {
  max-width: 100%;
  border-radius: 6px;
  margin: 6px 0;
}

.attachments {
  margin-bottom: 10px;
}

.printed {
  margin-bottom: 10px;
}

.printed-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.printed-item {
  width: 160px;
}

.printed-item img {
  width: 100%;
  height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #eee;
  background: #000;
  cursor: zoom-in;
}

.printed-caption {
  font-size: 12px;
  color: #555;
  margin-top: 4px;
  word-break: break-all;
}

.printed-empty {
  color: #888;
  font-size: 13px;
  margin-top: 6px;
}

.attach-upload {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.attach-upload input[type="file"] {
  font-size: 13px;
}

.attach-btn {
  background: #1976d2;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}

.attach-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.attach-msg {
  font-size: 12px;
  color: #666;
}

.attach-msg.error {
  color: #b00020;
}

.attach-list {
  list-style: none;
  padding-left: 0;
  margin: 10px 0 0;
}

.attach-list li {
  margin: 4px 0;
  font-size: 13px;
}

.attach-list a {
  color: #1976d2;
  text-decoration: none;
}

.attach-list a:hover {
  text-decoration: underline;
}

.attach-empty {
  color: #888;
}

.instances .inst-card {
  border: 1px solid #e6e6e6;
  padding: 12px;
  border-radius: 10px;
  margin-bottom: 12px;
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}

.instances .inst-card:hover {
  box-shadow: 0 6px 18px rgba(0,0,0,0.08);
  transform: translateY(-2px);
}

.inst-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 13px;
  color: #555;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  background: #f7f7f7;
  border: 1px solid #eee;
}

.meta-item:hover {
  background: #eef5ff;
  border-color: #d0e0ff;
}

.meta-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  margin-left: 6px;
  border-radius: 12px;
  background: #e8f5e9;
  color: #1b5e20;
  font-size: 12px;
  border: 1px solid #c8e6c9;
}

.inst-download {
  margin-left: 6px;
  font-size: 12px;
  text-decoration: none;
  background: #1976d2;
  color: #fff;
  padding: 2px 8px;
  border-radius: 10px;
}

.inst-download:hover {
  background: #0f5fb6;
}

.inst-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 6px;
  font-size: 12px;
  text-decoration: none;
  background: #1976d2;
  color: #fff;
  padding: 4px 10px;
  border-radius: 12px;
  border: none;
  white-space: nowrap;
  font-weight: bold;
  font-family: inherit;
  cursor: pointer;
}

.inst-btn.alt {
  background: #e53935;
}

.inst-btn.alt:hover {
  background: #c62828;
}

.inst-btn:hover {
  opacity: 0.9;
}

.inst-thumb {
  width: 140px;
  height: 140px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid #eee;
  background: #000;
  cursor: zoom-in;
}

.plates {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.plate-item {
  width: 120px;
  border: 1px solid #eee;
  border-radius: 8px;
  padding: 6px;
  font-size: 12px;
}

.plate-item img {
  width: 100%;
  height: 70px;
  object-fit: contain;
  border-radius: 6px;
  background: #000;
  cursor: zoom-in;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.chip {
  display: inline-block;
  padding: 2px 8px 2px 6px;
  border-radius: 12px;
  font-size: 12px;
  background: #f0f0f0;
  border: 1px solid #e8e8e8;
}

.chip .color-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-right: 6px;
  border: 1px solid #ccc;
  vertical-align: middle;
}

.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 6px 0 12px;
}

.thumbs img {
  width: 82px;
  height: 82px;
  object-fit: cover;
  border-radius: 6px;
  border: 2px solid transparent;
}

.thumbs img.active {
  border-color: #1976d2;
  box-shadow: 0 0 6px rgba(25, 118, 210, 0.6);
}

.zoomable {
  cursor: zoom-in;
}

.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.8);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 999999;
}

.lightbox img {
  max-width: 90vw;
  max-height: 90vh;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0,0,0,0.4);
}

.lightbox.show {
  display: flex;
}
.carousel {
  position: relative;
  margin: 10px 0 20px;
  overflow: hidden;
  border-radius: 8px;
  background: #000;
}

.carousel-track {
  display: flex;
  transition: transform 0.3s ease;
}

.carousel img {
  width: 100%;
  max-height: 480px;
  object-fit: contain;
  flex-shrink: 0;
  background: #000;
}

.carousel-btn {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 32px;
  height: 32px;
  border-radius: 16px;
  border: none;
  background: rgba(0,0,0,0.45);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.carousel-btn:hover {
  background: rgba(0,0,0,0.7);
}

.carousel-btn.prev {
  left: 10px;
}

.carousel-btn.next {
  right: 10px;
}
""".strip()


def normalize_stats(meta: dict) -> dict:
    stats = meta.get("stats") or meta.get("counts") or {}
    likes = stats.get("likes") or stats.get("like") or 0
    favorites = stats.get("favorites") or stats.get("favorite") or 0
    downloads = stats.get("downloads") or stats.get("download") or 0
    prints = stats.get("prints") or stats.get("print") or 0
    views = stats.get("views") or stats.get("read") or stats.get("reads") or 0
    return {
        "likes": likes,
        "favorites": favorites,
        "downloads": downloads,
        "prints": prints,
        "views": views,
    }


def normalize_author(meta: dict) -> dict:
    author_raw = meta.get("author")
    if isinstance(author_raw, str):
        return {"name": author_raw, "url": "", "avatar": None}
    if not isinstance(author_raw, dict):
        return {"name": "", "url": "", "avatar": None}

    avatar_local = author_raw.get("avatarLocal") or author_raw.get("avatar_local")
    avatar_rel = author_raw.get("avatarRelPath") or author_raw.get("avatar_local_path")
    if not avatar_rel and avatar_local:
        avatar_rel = f"images/{avatar_local}"

    return {
        "name": author_raw.get("name") or "",
        "url": author_raw.get("url") or "",
        "avatar": avatar_rel,
    }


def normalize_images(meta: dict) -> dict:
    images_raw = meta.get("images")
    design = []
    summary = []
    cover = None

    def to_name(item):
        if not item:
            return None
        return Path(item).name

    if isinstance(images_raw, dict):
        design = [to_name(x) for x in images_raw.get("design", []) if to_name(x)]
        summary = [to_name(x) for x in images_raw.get("summary", []) if to_name(x)]
        cover = to_name(images_raw.get("cover"))
    elif isinstance(images_raw, list):
        design = [to_name(x) for x in images_raw if to_name(x)]

    if not design and meta.get("designImages"):
        for item in meta.get("designImages", []):
            if isinstance(item, dict):
                val = item.get("fileName") or item.get("localName") or item.get("relPath")
                name = to_name(val)
                if name:
                    design.append(name)

    if not summary and meta.get("summaryImages"):
        for item in meta.get("summaryImages", []):
            if isinstance(item, dict):
                val = item.get("fileName") or item.get("relPath")
                name = to_name(val)
                if name:
                    summary.append(name)
            elif isinstance(item, str):
                name = to_name(item)
                if name:
                    summary.append(name)

    if not cover:
        cover_info = meta.get("cover") or {}
        cover = to_name(cover_info.get("relPath") or cover_info.get("localName"))

    return {"design": design, "summary": summary, "cover": cover}


def format_duration(seconds):
    try:
        sec = int(seconds)
    except Exception:
        return ""
    hours = sec / 3600.0
    if hours >= 1:
        return f"{hours:.1f} h"
    mins = sec / 60.0
    return f"{mins:.1f} min"


def format_date(date_str):
    try:
        if not date_str:
            return ""
        clean = date_str.replace("Z", "+00:00") if str(date_str).endswith("Z") else str(date_str)
        return str(datetime.fromisoformat(clean).date())
    except Exception:
        return date_str or ""


def _escape_json_for_inline_script(json_text: str) -> str:
    """
    将 JSON 文本安全注入 <script>，避免 </script>（任意大小写变体）截断脚本。
    """
    if not json_text:
        return "{}"
    return (
        json_text
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )



def build_fallback_index_html(meta: dict, assets: dict = None) -> str:
    title = str(meta.get("title") or meta.get("name") or "MakerHub Archive")
    author = normalize_author(meta)
    stats = normalize_stats(meta)
    images = normalize_images(meta)
    summary_payload = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
    summary_text = str(summary_payload.get("text") or summary_payload.get("raw") or meta.get("description") or "").strip()
    hero_name = images.get("cover") or (images.get("design") or [None])[0] or (images.get("summary") or [None])[0]
    hero_html = (
        f'<img src="./images/{escape(hero_name)}" alt="{escape(title)}" style="width:100%;border-radius:16px;object-fit:cover;background:#f3f4f6;" />'
        if hero_name
        else '<div style="height:260px;border-radius:16px;background:#f3f4f6;"></div>'
    )
    gallery_files = list(dict.fromkeys((images.get("design") or []) + (images.get("summary") or [])))
    gallery_html = "".join(
        f'<img src="./images/{escape(name)}" alt="{escape(title)}" style="width:100%;border-radius:14px;background:#f3f4f6;" />'
        for name in gallery_files
    ) or '<p style="color:#6b7280;">当前没有离线图片。</p>'
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    instances_html = "".join(
        f'<li><a href="./instances/{escape(str(item.get("fileName") or ""))}">{escape(str(item.get("title") or item.get("name") or item.get("fileName") or "实例文件"))}</a></li>'
        for item in instances
        if str(item.get("fileName") or "").strip()
    ) or '<li>当前没有可用 3MF 文件。</li>'
    attachments = meta.get("attachments") if isinstance(meta.get("attachments"), list) else []
    attachments_html = "".join(
        f'<li><a href="./file/{escape(str(item.get("localName") or ""))}">{escape(str(item.get("name") or item.get("localName") or "附件"))}</a></li>'
        for item in attachments
        if str(item.get("localName") or "").strip()
    )
    attachments_section = (
        f"""
        <section>
          <h2>附件</h2>
          <ul>{attachments_html}</ul>
        </section>
        """
        if attachments_html
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)} - makerhub archive</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fb; color: #111827; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 40px 24px 64px; }}
    .hero {{ display: grid; gap: 24px; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 420px); align-items: start; }}
    .panel {{ background: #fff; border-radius: 20px; padding: 24px; box-shadow: 0 20px 60px rgba(15, 23, 42, 0.08); }}
    .meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 20px; }}
    .meta div {{ border-radius: 14px; background: #f8fafc; padding: 14px 16px; }}
    .gallery {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); margin-top: 24px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    p {{ line-height: 1.7; }}
    ul {{ margin: 0; padding-left: 18px; line-height: 1.8; }}
    a {{ color: #2563eb; text-decoration: none; }}
    @media (max-width: 900px) {{ .hero {{ grid-template-columns: 1fr; }} .meta {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="panel">{hero_html}</div>
      <div class="panel">
        <p style="margin:0 0 8px;color:#2563eb;font-weight:600;">makerhub 离线归档</p>
        <h1>{escape(title)}</h1>
        <p style="margin:0;color:#6b7280;">作者：{escape(author.get("name") or "未知作者")}</p>
        <div class="meta">
          <div><strong>下载</strong><br />{escape(str(stats.get("downloads") or 0))}</div>
          <div><strong>点赞</strong><br />{escape(str(stats.get("likes") or 0))}</div>
          <div><strong>打印</strong><br />{escape(str(stats.get("prints") or 0))}</div>
          <div><strong>收藏</strong><br />{escape(str(stats.get("favorites") or 0))}</div>
        </div>
        <p style="margin-top:20px;">{escape(summary_text or "当前没有离线简介。")}</p>
      </div>
    </section>
    <section class="panel" style="margin-top:24px;">
      <h2>图片</h2>
      <div class="gallery">{gallery_html}</div>
    </section>
    <section class="panel" style="margin-top:24px;">
      <h2>3MF 文件</h2>
      <ul>{instances_html}</ul>
    </section>
    {attachments_section}
  </main>
</body>
</html>
"""


def build_index_html(meta: dict, assets: dict = None) -> str:
    """基于 CSR 架构的离线 HTML 生成器，读取 model.html 骨架并注入数据和源码。"""
    app_dir = Path(__file__).parent
    template_path = app_dir / "templates" / "model.html"
    variables_css_path = app_dir / "static" / "css" / "variables.css"
    components_css_path = app_dir / "static" / "css" / "components.css"
    model_css_path = app_dir / "static" / "css" / "model.css"
    model_js_path = app_dir / "static" / "js" / "model.js"

    required_paths = [
        template_path,
        variables_css_path,
        components_css_path,
        model_css_path,
        model_js_path,
    ]
    missing_paths = [path for path in required_paths if not path.exists()]
    if missing_paths:
        log("离线模板缺失，使用回退 HTML：", ", ".join(str(path) for path in missing_paths))
        return build_fallback_index_html(meta, assets)

    with template_path.open("r", encoding="utf-8") as f:
        html = f.read()

    with variables_css_path.open("r", encoding="utf-8") as f:
        variables_css = f.read()

    with components_css_path.open("r", encoding="utf-8") as f:
        components_css = f.read()

    import re
    with model_css_path.open("r", encoding="utf-8") as f:
        model_css = f.read()
        model_css = re.sub(
            r"@import\s+url\(['\"]?/static/css/(?:variables|components)\.css[^)]*\)\s*;?",
            "",
            model_css,
            flags=re.I,
        )

    with model_js_path.open("r", encoding="utf-8") as f:
        model_js = f.read()

    # 离线归档页移除外部依赖（favicon/static、FontAwesome CDN）
    html = re.sub(
        r'<link[^>]*rel=["\']icon["\'][^>]*>\s*',
        "",
        html,
        flags=re.I,
    )
    html = re.sub(
        r'<link[^>]*href=["\']https?://[^"\']*font-awesome[^"\']*["\'][^>]*>\s*',
        "",
        html,
        flags=re.I,
    )

    # 内联 CSS（宽松匹配属性顺序/单双引号）
    variables_inline = f"<style>\n{variables_css}\n</style>"
    model_inline = f"<style>\n{components_css}\n{model_css}\n</style>"

    html, var_count = re.subn(
        r'<link[^>]*href=["\']/static/css/variables\.css[^"\']*["\'][^>]*>',
        lambda _: variables_inline,
        html,
        count=1,
        flags=re.I,
    )
    if var_count == 0:
        html, _ = re.subn(r"</head>", lambda _: variables_inline + "\n</head>", html, count=1, flags=re.I)

    html, model_count = re.subn(
        r'<link[^>]*href=["\']/static/css/model\.css[^"\']*["\'][^>]*>',
        lambda _: model_inline,
        html,
        count=1,
        flags=re.I,
    )
    if model_count == 0:
        html, _ = re.subn(r"</head>", lambda _: model_inline + "\n</head>", html, count=1, flags=re.I)

    # 注入离线 Meta 和 JS (使用 JSON 安全的直接注入)
    import json
    meta_json_str = json.dumps(meta, ensure_ascii=False)
    meta_json_str = _escape_json_for_inline_script(meta_json_str)
    injection_script = f"\n<script>\nwindow.__OFFLINE_META__ = {meta_json_str};\n</script>\n"
    js_replacement = f"{injection_script}<script>\n{model_js}\n</script>"

    html, count = re.subn(
        r'<script[^>]*src=["\']/static/js/model\.js[^"\']*["\'][^>]*>\s*</script>',
        lambda _: js_replacement,
        html,
        count=1,
        flags=re.I,
    )
    if count == 0:
        html, body_count = re.subn(
            r"</body>",
            lambda _: f"{js_replacement}\n</body>",
            html,
            count=1,
            flags=re.I,
        )
        if body_count == 0:
            html += js_replacement

    return html


def rebuild_once(meta_path: Path, progress_callback=None):
    with meta_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    base_name = meta.get("baseName") or meta_path.stem.replace("_meta", "")
    if meta_path.name == "meta.json" and meta_path.parent.name == base_name:
        work_dir = meta_path.parent
    else:
        work_dir = meta_path.parent / base_name
    ensure_dir(work_dir)

    emit_progress(progress_callback, 84, f"正在整理归档目录：{base_name}")
    log("归档生成页面:", base_name)

    # 1. 写/移动 meta.json 到目标目录，仅保留目标目录一份
    target_meta = work_dir / "meta.json"
    if not target_meta.exists():
        shutil.move(str(meta_path), str(target_meta))
    else:
        if meta_path.resolve() != target_meta.resolve() and meta_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass

    # 2. 准备子目录
    images_dir = work_dir / "images"
    instances_dir = work_dir / "instances"
    files_dir = work_dir / "file"
    ensure_dir(images_dir)
    ensure_dir(instances_dir)
    ensure_dir(files_dir)

    # 3. 移动 screenshot
    screenshot_file = None
    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_screenshot.*", "screenshot.*"]):
        dst = work_dir / f"screenshot{p.suffix.lower()}"
        if move_or_replace(p, dst, "screenshot"):
            screenshot_file = dst
            break
    if not screenshot_file:
        existing = next(iter(work_dir.glob("screenshot.*")), None)
        if existing:
            screenshot_file = existing

    # 4. 封面图 & 作者头像 & design & summary images
    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_cover.*", "cover.*"]):
        dst = images_dir / f"cover{p.suffix.lower()}"
        move_or_replace(p, dst, "cover")
        break

    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_author_avatar.*", "author_avatar.*"]):
        dst = images_dir / f"author_avatar{p.suffix.lower()}"
        move_or_replace(p, dst, "author_avatar")
        break

    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_design_*", "design_*"]):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "design 图片")

    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_summary_img_*", "summary_img_*"]):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "summary 图片")

    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_comment_*", "comment_*"]):
        new_name = strip_prefix(p.name, base_name)
        dst = images_dir / new_name
        move_or_replace(p, dst, "comment 资源")

    # 5. 实例配图/plate 缩略图
    for p in glob_with_prefix_or_plain(meta_path.parent, base_name, ["_inst*_*"]):
        new_name = p.name
        dst = images_dir / new_name
        move_or_replace(p, dst, "实例图片")

    # 6. 下载 3MF 到 instances 目录
    instances = meta.get("instances", []) or []
    inst_files = []
    meta_changed = False
    total_instance_steps = max(len(instances), 1)
    for inst in instances:
        url = inst.get("downloadUrl")
        if not url:
            continue
        fn = choose_unique_instance_filename(inst, instances, instances_dir, inst.get("name") or "")
        if str(inst.get("fileName") or "").strip() != fn:
            inst["fileName"] = fn
            meta_changed = True
        dest = instances_dir / fn

        download_file(
            REBUILD_SESSION,
            url,
            dest,
            max_duration=BINARY_TRANSFER_TIMEOUT_SECONDS,
        )
        inst_files.append({
            "id": inst.get("id"),
            "title": inst.get("title") or inst.get("name") or str(inst.get("id")),
            "file": dest.name,
        })
        processed_instances = len(inst_files)
        emit_progress(
            progress_callback,
            84 + min(int(processed_instances * 10 / total_instance_steps), 10),
            f"正在下载实例文件（{processed_instances}/{len(instances)}）",
            {"current": processed_instances, "total": len(instances)},
        )

    attachments = meta.get("attachments") or []
    attachment_files = []
    if isinstance(attachments, list):
        used_names = set()
        for idx, att in enumerate(attachments, start=1):
            if not isinstance(att, dict):
                continue
            url = str(att.get("url") or "").strip()
            if not url:
                continue
            preferred = str(att.get("localName") or att.get("name") or "").strip()
            if not preferred:
                preferred = _attachment_name_from_url(url, f"attachment_{idx}.{pick_ext_from_url(url, 'bin')}")
            local_name = _dedupe_attachment_local_name(preferred, used_names)
            if str(att.get("localName") or "") != local_name:
                att["localName"] = local_name
                meta_changed = True
            rel_path = f"file/{local_name}"
            if str(att.get("relPath") or "") != rel_path:
                att["relPath"] = rel_path
                meta_changed = True
            dest = files_dir / local_name
            download_file(
                REBUILD_SESSION,
                url,
                dest,
                max_duration=BINARY_TRANSFER_TIMEOUT_SECONDS,
            )
            attachment_files.append(local_name)
            emit_progress(
                progress_callback,
                95,
                f"正在下载附件（{len(attachment_files)}/{len(attachments)}）",
                {"current": len(attachment_files), "total": len(attachments)},
            )

    offline = meta.get("offlineFiles")
    if not isinstance(offline, dict):
        offline = {}
        meta["offlineFiles"] = offline
    if offline.get("attachments") != attachment_files:
        offline["attachments"] = attachment_files
        meta_changed = True

    if attachment_files:
        index_payload = {
            "files": attachment_files,
            "items": [{"name": name, "size": (files_dir / name).stat().st_size if (files_dir / name).exists() else 0} for name in attachment_files],
        }
        (files_dir / "_index.json").write_text(json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 若实例文件名或附件信息发生调整，写回 meta.json，保证元数据与磁盘一致
    if meta_changed:
        target_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7. 生成 index.html（基于 templates/model.html + static/css/js 内联）
    design_files = sorted([p.name for p in images_dir.glob("design_*")])
    cover_file = next(iter(images_dir.glob("cover.*")), None)
    avatar_file = next(iter(images_dir.glob("author_avatar.*")), None)

    hero_file = screenshot_file or cover_file or (images_dir / design_files[0] if design_files else None)
    hero_rel = hero_file.relative_to(work_dir).as_posix() if hero_file else "screenshot.png"

    assets = {
        "design_files": design_files,
        "hero": f"./{hero_rel}",
        "avatar": f"./{avatar_file.relative_to(work_dir).as_posix()}" if avatar_file else None,
        "collected_date": datetime.now().strftime("%Y-%m-%d"),
        "instance_files": inst_files,
        "base_name": base_name,
    }

    index_html = build_index_html(meta, assets)
    (work_dir / "index.html").write_text(index_html, encoding="utf-8")

    emit_progress(progress_callback, 98, "正在生成离线页面")
    log("完成归档:", work_dir)


def archive_model(
    url: str,
    cookie: str,
    download_dir: Path,
    logs_dir: Path,
    logger=None,
    existing_root: Optional[Path] = None,
    progress_callback=None,
):
    """
    对外主入口：采集 + 下载文件 + 生成 meta/index.html/style.css
    返回: {base_name, work_dir, missing_3mf, action}
    """
    # 采集阶段
    out_root = download_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (MW-Fetcher)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    raw_cookie_header = (cookie or "").strip()
    parsed_cookies = parse_cookies(raw_cookie_header)
    sess.cookies.update(parsed_cookies)

    fetch_url = url.split("#", 1)[0]
    emit_progress(progress_callback, 5, "准备抓取模型页面")
    log(logger, "获取页面:", fetch_url)
    log(logger, "请求头:", sess.headers)
    log(logger, "请求 Cookie 头(前 300 字符):", raw_cookie_header[:300])

    # 优先用 requests 拉取页面，失败再回退 curl
    emit_progress(progress_callback, 12, "正在获取模型页面")
    html_text = fetch_html_with_requests(sess, fetch_url, raw_cookie_header)
    if not html_text:
        html_text = fetch_html_with_curl(fetch_url, raw_cookie_header)
    elif "__NEXT_DATA__" not in html_text and "__NUXT__" not in html_text:
        log(logger, "requests 页面未包含 __NEXT_DATA__，尝试 curl 回退")
        html_text = fetch_html_with_curl(fetch_url, raw_cookie_header)

    if "__NEXT_DATA__" not in html_text and "__NUXT__" not in html_text:
        log(logger, "页面未包含 __NEXT_DATA__，前 300 字符:", (html_text or "")[:300])
    if _is_cloudflare_challenge(html_text):
        log(logger, "检测到 Cloudflare 验证页面，可能需要更新 cookie 中的 cf_clearance")

    design = None
    next_data = {}
    try:
        next_data = extract_next_data(html_text)
        design = extract_design_from_next_data(next_data)
        if design is None:
            log(logger, "未能从 __NEXT_DATA__ 定位 design，尝试 API 获取")
    except Exception as e:
        log(logger, "解析 __NEXT_DATA__ 失败，尝试 API 获取:", e)

    api_host_hint = _extract_api_host(html_text)
    if design is None:
        emit_progress(progress_callback, 22, "页面数据不足，正在回退接口抓取")
        design = fetch_design_from_api(sess, raw_cookie_header, fetch_url, api_host_hint=api_host_hint)

    if design is None:
        if _is_cloudflare_challenge(html_text):
            raise RuntimeError("页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试")
        raise RuntimeError("未能解析模型数据，请确认 cookie/页面结构")

    design["url"] = url
    emit_progress(progress_callback, 30, "已解析模型信息，准备下载资源")

    design_id = design.get("id") or _parse_design_id(url)
    if design_id is None:
        raise RuntimeError("未获取到模型 ID")
    title = design.get("title") or "model"
    base_name, action = choose_archive_base_name(design_id, title, existing_root=existing_root)
    work_dir = out_root / base_name
    existing_meta = load_existing_meta(work_dir) if action == "updated" else {}
    images_dir = work_dir / "images"
    ensure_dir(images_dir)

    author = extract_author(design, html_text)
    if author.get("avatarUrl"):
        ext = pick_ext_from_url(author["avatarUrl"])
        fname = f"author_avatar.{ext}"
        try:
            download_file(
                sess,
                author["avatarUrl"],
                images_dir / fname,
                max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
            )
            author["avatarLocal"] = fname
            author["avatarRelPath"] = f"images/{fname}"
        except Exception as exc:
            log(logger, "作者头像下载失败，保留原始链接：", author["avatarUrl"], exc)

    emit_progress(progress_callback, 40, "正在整理摘要与设计图片")
    summary = parse_summary(
        design,
        base_name,
        sess,
        images_dir,
        progress_callback=progress_callback,
        progress_start=40,
        progress_end=45,
    )
    design_images, cover_meta = collect_design_images(
        design,
        sess,
        images_dir,
        base_name,
        progress_callback=progress_callback,
        progress_start=45,
        progress_end=50,
    )
    attachments = extract_design_attachments(design)
    comments_bundle = collect_comments(
        next_data,
        design,
        sess,
        images_dir,
        progress_callback=progress_callback,
        progress_start=50,
        progress_end=55,
    )
    emit_progress(progress_callback, 55, "摘要、图片与评论整理完成")

    parsed_origin = urlparse(fetch_url)
    origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}" if parsed_origin.scheme and parsed_origin.netloc else "https://makerworld.com.cn"

    inst_list = []
    planned_instances_dir = work_dir / "instances"
    existing_instance_index = _build_existing_instance_index(existing_meta.get("instances"))
    extracted_instances = extract_instances(design)
    total_instances = max(len(extracted_instances), 1)
    payload_hint_hits = 0
    existing_hint_hits = 0
    fetched_hint_hits = 0
    for idx, inst in enumerate(extracted_instances, start=1):
        inst_id = inst.get("id") or inst.get("instanceId")
        if inst_id is None:
            continue
        existing_inst = _find_existing_instance(inst, existing_instance_index)
        emit_progress(
            progress_callback,
            55 + min(int(idx * 20 / total_instances), 20),
            f"正在处理实例信息（{idx}/{len(extracted_instances)}）",
            {"current": idx, "total": len(extracted_instances)},
        )
        plates, pics = collect_instance_media(
            inst,
            sess,
            images_dir,
            base_name,
            download_assets=False,
            existing_instance=existing_inst,
        )
        hinted_name, hinted_url, hinted_api_url = _extract_instance_download_hint(inst)
        api_url = (
            hinted_api_url
            or inst.get("apiUrl")
            or existing_inst.get("apiUrl")
            or f"{origin}/api/v1/design-service/instance/{inst_id}/f3mf?type=download&fileType="
        )
        name3mf = hinted_name or str(existing_inst.get("name") or "").strip()
        url3mf = hinted_url or str(existing_inst.get("downloadUrl") or "").strip()
        used_api_url = api_url
        failure_info = {"state": "available", "message": ""} if url3mf else {"state": "missing", "message": "未获取到 3MF 下载地址。"}
        if hinted_url:
            payload_hint_hits += 1
        elif url3mf:
            existing_hint_hits += 1
        else:
            name3mf, url3mf, used_api_url, failure_info = fetch_instance_3mf(
                sess,
                inst_id,
                raw_cookie_header,
                api_url,
                api_host_hint=api_host_hint,
                origin=origin,
            )
            if url3mf:
                fetched_hint_hits += 1
        failure_state = str((failure_info or {}).get("state") or "").strip()
        failure_message = str((failure_info or {}).get("message") or "").strip()
        inst_record = {
            "id": inst_id,
            "profileId": inst.get("profileId") or inst.get("profile_id") or inst.get("profileID") or existing_inst.get("profileId") or existing_inst.get("profile_id") or existing_inst.get("profileID"),
            "title": inst.get("title") or inst.get("name") or existing_inst.get("title") or existing_inst.get("name"),
            "titleTranslated": inst.get("titleTranslated") or existing_inst.get("titleTranslated") or "",
            "publishTime": inst.get("publishTime") or inst.get("publishedAt") or existing_inst.get("publishTime") or existing_inst.get("publishedAt") or "",
            "machine": inst.get("machine") or inst.get("machineName") or inst.get("printerModel") or inst.get("printer") or inst.get("device") or existing_inst.get("machine") or existing_inst.get("machineName") or existing_inst.get("printerModel") or existing_inst.get("printer") or existing_inst.get("device") or "",
            "time": inst.get("time") or inst.get("timeText") or inst.get("durationText") or existing_inst.get("time") or existing_inst.get("timeText") or existing_inst.get("durationText") or "",
            "timeText": inst.get("timeText") or existing_inst.get("timeText") or "",
            "durationText": inst.get("durationText") or existing_inst.get("durationText") or "",
            "printTimeSeconds": inst.get("printTimeSeconds") or inst.get("duration") or existing_inst.get("printTimeSeconds") or existing_inst.get("duration") or 0,
            "rating": inst.get("rating") or inst.get("score") or inst.get("stars") or existing_inst.get("rating") or existing_inst.get("score") or existing_inst.get("stars") or "",
            "downloadCount": inst.get("downloadCount") or existing_inst.get("downloadCount") or 0,
            "printCount": inst.get("printCount") or existing_inst.get("printCount") or 0,
            "prediction": inst.get("prediction"),
            "weight": inst.get("weight"),
            "materialCnt": inst.get("materialCnt"),
            "materialColorCnt": inst.get("materialColorCnt"),
            "needAms": inst.get("needAms"),
            "cover": inst.get("cover") or inst.get("coverUrl") or existing_inst.get("cover") or existing_inst.get("coverUrl") or "",
            "previewImage": inst.get("previewImage") or existing_inst.get("previewImage") or "",
            "thumbnail": inst.get("thumbnail") or existing_inst.get("thumbnail") or "",
            "thumbnailUrl": inst.get("thumbnailUrl") or existing_inst.get("thumbnailUrl") or "",
            "plates": plates,
            "pictures": pics,
            "instanceFilaments": inst.get("instanceFilaments") or existing_inst.get("instanceFilaments") or [],
            "summary": inst.get("summary") or existing_inst.get("summary") or "",
            "summaryTranslated": inst.get("summaryTranslated") or existing_inst.get("summaryTranslated") or "",
            "name": name3mf,
            "downloadUrl": url3mf,
            "apiUrl": used_api_url or api_url,
            "downloadState": "" if url3mf else failure_state,
            "downloadMessage": "" if url3mf else failure_message,
            "fileName": str(existing_inst.get("fileName") or "").strip(),
        }
        # 在构建 meta 阶段就写入 fileName，保证后续语义清晰：
        # title=展示名，name=来源名，fileName=本地真实文件名。
        inst_list.append(inst_record)
        inst_record["fileName"] = choose_unique_instance_filename(
            inst_record,
            inst_list,
            planned_instances_dir,
            inst_record.get("name") or "",
        )
    log(
        logger,
        "实例处理完成:",
        f"payload_hint={payload_hint_hits}",
        f"existing_hint={existing_hint_hits}",
        f"api_fetch={fetched_hint_hits}",
        f"total={len(inst_list)}",
    )

    meta = build_meta(
        design,
        summary,
        design_images,
        cover_meta,
        inst_list,
        author,
        base_name,
        attachments=attachments,
        comments_bundle=comments_bundle,
    )
    existing_collect_date = existing_meta.get("collectDate")
    if existing_collect_date not in (None, "", 0, "0"):
        meta["collectDate"] = existing_collect_date
    meta_path = work_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_progress(progress_callback, 78, "元数据已生成，准备落盘")
    log(logger, "已保存 meta:", meta_path)

    # 归档整理
    log_section("归档整理阶段")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        rebuild_once(meta_path, progress_callback=progress_callback)
    except Exception as e:
        log(logger, "归档/生成本地页面失败:", e)

    # 缺失 3MF 记录（仅记录，没有下载 3mf）
    missing_3mf = [inst for inst in inst_list if not inst.get("downloadUrl")]
    if missing_3mf:
        logs_dir.mkdir(parents=True, exist_ok=True)
        missing_log = logs_dir / "missing_3mf.log"
        with missing_log.open("a", encoding="utf-8") as f:
            for m in missing_3mf:
                f.write(
                    f"{datetime.now().isoformat()}\t{base_name}\t{m['id']}\t{m.get('title','')}\t"
                    f"{m.get('downloadMessage') or m.get('downloadState') or '未获取到 3MF 下载地址'}\n"
                )
        log(logger, "缺失 3MF 已记录:", missing_log)

    work_dir = meta_path.parent
    emit_progress(progress_callback, 100, "归档完成")
    return {
        "base_name": base_name,
        "work_dir": str(work_dir.resolve()),
        "missing_3mf": missing_3mf,
        "action": action,
        "model_id": design_id,
    }


if __name__ == "__main__":
    log("此模块用于被导入调用，不建议直接运行。")
    sys.exit(0)
