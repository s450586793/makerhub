import hashlib
import re
from typing import Any, Optional


_COMMENT_STRONG_MARKER_KEYS = (
    "commentId", "rootCommentId", "replyCount", "subCommentCount", "commentTime",
    "commentType", "isTop", "isPinned", "praiseCount", "likeCount",
)
_COMMENT_WEAK_MARKER_KEYS = ("rating", "score", "star", "starLevel")
_COMMENT_PLACEHOLDER_CONTENT = {
    "模型描述", "评论", "描述", "回复", "modeldescription", "comment", "comments",
    "description", "review", "reviews", "reply", "replies",
}
_COMMENT_CHILD_KEYS = (
    "replies", "children", "subComments", "subCommentList", "subCommentVos",
    "subCommentVOList", "replyList", "replys", "replyVos", "replyVOList",
    "commentReplies", "commentReply", "commentReplyVos", "commentReplyList",
    "instRatingReply", "instRatingReplies", "ratingReply", "ratingReplies",
    "replyComments", "replyInfoList", "childComments",
)
_COMMENT_CHILD_CONTAINER_KEYS = ("items", "list", "rows", "records", "results", "nodes", "edges", "data")
_COMMENT_CHILD_NODE_KEYS = ("node", "item", "record", "comment", "reply", "child")
_COMMENT_REPLY_DIRECT_KEYS = (
    "replyToName", "replyUserName", "replyNickName", "targetUserName", "parentAuthor",
    "parentUserName", "toUserName", "beRepliedUserName",
)
_COMMENT_REPLY_USER_KEYS = (
    "replyToUser", "replyUser", "targetUser", "beRepliedUser", "parentUser", "atUser",
)
_COMMENT_SECTION_KEYWORDS = (
    "comment", "comments", "review", "reviews", "reply", "replies", "thread", "threads", "feedback",
)
_COMMENT_SEARCH_ROOT_KEYS = {
    "props", "pageprops", "data", "pagedata", "payload", "result", "detail", "model",
    "design", "designextension", "query", "queries", "apollo", "state",
}
_COMMENT_LIST_CONTAINER_KEYS = {"items", "list", "rows", "records", "results", "edges", "nodes", "data"}


def _comment_text_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        for key in ("text", "content", "value", "raw", "html", "message"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return " ".join(nested.split())
    return ""


def _comment_numeric(value: Any) -> int:
    try:
        return 0 if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return 0


def _comment_author_name(node: dict) -> str:
    user = node.get("user") or node.get("author") or node.get("creator") or node.get("commentUser") or {}
    user = user if isinstance(user, dict) else {}
    return str(
        user.get("nickname") or user.get("nickName") or user.get("name") or user.get("username")
        or user.get("userName") or node.get("nickname") or node.get("nickName") or node.get("userName")
        or node.get("username") or node.get("authorName") or node.get("creatorName") or ""
    ).strip()


def _comment_created_at_value(node: dict) -> str:
    return str(
        node.get("commentTime") or node.get("createTime") or node.get("createdAt")
        or node.get("publishTime") or node.get("time") or ""
    ).strip()


def _compact_comment_content(value: str) -> str:
    return re.sub(r"[\s:：/|_\\-]+", "", str(value or "").strip().casefold())


def _is_placeholder_comment_payload(node: dict) -> bool:
    content = next(
        (_comment_text_value(node.get(key)) for key in ("content", "commentContent", "comment", "message", "text", "body", "description") if _comment_text_value(node.get(key))),
        "",
    )
    if _compact_comment_content(content) not in _COMMENT_PLACEHOLDER_CONTENT:
        return False
    images = node.get("images")
    return not (
        _comment_author_name(node) not in ("", "匿名用户")
        or _comment_created_at_value(node)
        or isinstance(images, list) and bool(images)
    )


def _is_rating_comment_node(node: dict) -> bool:
    return any(key in node for key in _COMMENT_WEAK_MARKER_KEYS) and any(
        key in node for key in ("instanceId", "instInfo", "successPrinted", "instRatingReply")
    )


def _is_rating_reply_node(node: dict) -> bool:
    return "ratingId" in node and any(key in node for key in ("replyId", "replyUid", "creator", "atUser"))


def _extract_comment_image_candidates(node: dict) -> list[dict]:
    found: list[dict] = []
    for key in ("pictures", "images", "imageList", "imageUrls", "commentPictures", "commentImages", "medias", "mediaList", "photos"):
        for item in node.get(key) if isinstance(node.get(key), list) else []:
            url = item.strip() if isinstance(item, str) else str(
                item.get("url") or item.get("imageUrl") or item.get("src") or item.get("originalUrl") or item.get("downloadUrl") or ""
            ).strip() if isinstance(item, dict) else ""
            if url:
                found.append({"url": url})
    return found


def _comment_child_nodes(node: dict) -> list[dict]:
    def looks_like_comment(value: object) -> bool:
        return isinstance(value, dict) and any(key in value for key in (
            "id", "commentId", "rootCommentId", "content", "commentContent", "comment", "message", "text",
            "replyCount", "ratingId", "subCommentCount", "childrenCount", "commentTime", "createTime", "createdAt",
        ))

    def extract(value: object, depth: int = 0) -> list[dict]:
        if depth > 4 or value is None:
            return []
        if isinstance(value, list):
            return [item for value_item in value for item in (
                [value_item] if looks_like_comment(value_item) else extract(value_item, depth + 1) if isinstance(value_item, dict) else []
            )]
        if isinstance(value, dict):
            if looks_like_comment(value):
                return [value]
            for key in (*_COMMENT_CHILD_CONTAINER_KEYS, *_COMMENT_CHILD_NODE_KEYS):
                nested = extract(value.get(key), depth + 1)
                if nested:
                    return nested
        return []

    children: list[dict] = []
    seen: set[int] = set()
    for key in _COMMENT_CHILD_KEYS:
        for child in extract(node.get(key)):
            if id(child) not in seen:
                seen.add(id(child))
                children.append(child)
    return children


def _reply_user_payload(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("nickname", "nickName", "name", "username", "userName", "avatarUrl", "avatar", "headImg", "url", "homepage")
        if value.get(key) not in (None, "", [], {})
    }


def _comment_reply_items(node: object) -> list[dict]:
    return [item for item in node.get("replies", []) if isinstance(item, dict)] if isinstance(node, dict) else []


def _comment_reply_count(node: dict) -> int:
    return _comment_numeric(node.get("replyCount") or node.get("reply_count") or node.get("subCommentCount") or node.get("childrenCount"))


def _comment_reply_to(node: dict) -> str:
    for key in _COMMENT_REPLY_DIRECT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in _COMMENT_REPLY_USER_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            for field in ("nickname", "nickName", "name", "username", "userName"):
                candidate = str(value.get(field) or "").strip()
                if candidate:
                    return candidate
    return ""


def _looks_like_flat_reply_candidate(node: dict) -> bool:
    comment_type = str(node.get("commentType") or node.get("comment_type") or "").strip().lower()
    return bool(_comment_reply_to(node) or comment_type and comment_type not in {"0", "root", "comment", "main"})


def _comment_identity_key(node: dict) -> str:
    explicit = str(node.get("id") or node.get("commentId") or "").strip()
    if explicit:
        return explicit
    author = _comment_author_name(node)
    content = next((_comment_text_value(node.get(key)) for key in ("content", "commentContent", "comment", "message", "text", "body", "description") if _comment_text_value(node.get(key))), "")
    digest = hashlib.sha1("|".join((str(node.get("rootCommentId") or node.get("root_comment_id") or "").strip(), author, _comment_created_at_value(node), content)).encode("utf-8", errors="ignore")).hexdigest()
    return digest[:16]


def _merge_comment_items(existing: dict, fresh: dict) -> dict:
    merged = dict(existing)
    for key, value in fresh.items():
        if key != "replies" and value not in (None, "", [], {}):
            merged[key] = value
    replies = _merge_threaded_comment_list(_comment_reply_items(existing), _comment_reply_items(fresh))
    if replies or "replies" in existing or "replies" in fresh:
        merged["replies"] = replies
        merged["replyCount"] = max(len(replies), _comment_reply_count(existing), _comment_reply_count(fresh))
    return merged


def _merge_threaded_comment_list(existing_items: list[dict], fresh_items: list[dict]) -> list[dict]:
    merged: list[dict] = []
    by_key: dict[str, dict] = {}
    for item in [*existing_items, *fresh_items]:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        replies = _merge_threaded_comment_list([], _comment_reply_items(item))
        if replies:
            normalized["replies"] = replies
        elif "replies" in normalized:
            normalized["replies"] = []
        if _is_placeholder_comment_payload(normalized):
            continue
        key = _comment_identity_key(normalized)
        if key in by_key:
            merged_item = _merge_comment_items(by_key[key], normalized)
            by_key[key].clear()
            by_key[key].update(merged_item)
        else:
            merged.append(normalized)
            by_key[key] = normalized
    return merged


def _normalize_comment_candidate(node: dict, *, replies: Optional[list[dict]] = None) -> Optional[dict]:
    if not isinstance(node, dict):
        return None
    strong = any(key in node for key in _COMMENT_STRONG_MARKER_KEYS)
    weak = any(key in node for key in _COMMENT_WEAK_MARKER_KEYS)
    if not (strong or weak) or any(key in node for key in ("designExtension", "coverUrl", "downloadCount", "printCount", "instances")):
        return None
    content_keys = ("commentContent", "content", "comment", "message", "text", "body", "description") if strong else ("commentContent", "content", "comment", "message", "text", "body")
    content = next((_comment_text_value(node.get(key)) for key in content_keys if _comment_text_value(node.get(key))), "")
    images = _extract_comment_image_candidates(node)
    author_name = _comment_author_name(node)
    created_at = _comment_created_at_value(node)
    has_identity = bool(str(node.get("commentId") or node.get("rootCommentId") or "").strip() or created_at or author_name or node.get("avatarUrl") or node.get("avatar") or images)
    rating = max(0, min(5, _comment_numeric(node.get("rating") or node.get("score") or node.get("star") or node.get("starLevel"))))
    like_count = _comment_numeric(node.get("likeCount") or node.get("praiseCount"))
    reply_count = _comment_reply_count(node)
    if not content and not (_is_rating_comment_node(node) and has_identity and (rating > 0 or images)):
        return None
    if _compact_comment_content(content) in _COMMENT_PLACEHOLDER_CONTENT and not has_identity:
        return None
    if weak and not strong and not has_identity:
        return None
    if not has_identity and like_count <= 0 and reply_count <= 0 and rating <= 0 and len(content) <= 12:
        return None
    user = node.get("user") or node.get("author") or node.get("creator") or node.get("commentUser") or {}
    user = user if isinstance(user, dict) else {}
    reply_items = replies if isinstance(replies, list) else []
    comment_id = str(node.get("commentId") or node.get("id") or "").strip()
    rating_id = str(node.get("ratingId") or "").strip()
    root_id = str(node.get("rootCommentId") or "").strip()
    source = "rating" if _is_rating_comment_node(node) else "rating_reply" if _is_rating_reply_node(node) else ""
    if source == "rating":
        root_id = root_id or comment_id
    elif source == "rating_reply":
        root_id = root_id or rating_id
    payload = {
        "id": comment_id or hashlib.sha1(f"{root_id}|{author_name}|{created_at}|{content}".encode("utf-8", errors="ignore")).hexdigest()[:16],
        "author": {"name": author_name, "avatarUrl": str(user.get("avatarUrl") or user.get("avatar") or user.get("headImg") or node.get("avatarUrl") or node.get("avatar") or "").strip(), "avatarLocal": "", "avatarRelPath": "", "url": str(user.get("homepage") or user.get("url") or node.get("authorUrl") or "").strip()},
        "content": content, "createdAt": created_at, "likeCount": like_count,
        "replyCount": max(reply_count, len(reply_items)), "rating": rating, "badges": [], "images": images,
        "rootCommentId": root_id,
    }
    if node.get("isTop") or node.get("isPinned"):
        payload["badges"].append("置顶")
    if node.get("isBoost") or node.get("isBoosted"):
        payload["badges"].append("已助力")
    if node.get("designerReplied") or node.get("hasDesignerReply") or node.get("isOfficialReply"):
        payload["badges"].append("设计师已回复")
    if str(node.get("profileName") or node.get("profileTitle") or "").strip():
        payload["badges"].append(str(node.get("profileName") or node.get("profileTitle")).strip())
    if reply_items:
        payload["replies"] = reply_items
    if source:
        payload["commentSource"] = source
    if rating_id:
        payload["ratingId"] = rating_id
    elif source == "rating" and comment_id:
        payload["ratingId"] = comment_id
    if str(node.get("replyId") or "").strip():
        payload["replyId"] = str(node.get("replyId")).strip()
    for key in _COMMENT_REPLY_DIRECT_KEYS:
        if isinstance(node.get(key), str) and node[key].strip():
            payload[key] = node[key].strip()
    for key in _COMMENT_REPLY_USER_KEYS:
        value = _reply_user_payload(node.get(key))
        if value:
            payload[key] = value
    return payload


def _collect_comment_tree(node: object, seen: dict[str, dict], depth: int = 0) -> tuple[Optional[dict], bool]:
    if depth > 12 or not isinstance(node, dict):
        return None, False
    replies = [reply for child in _comment_child_nodes(node) for reply, is_new in [_collect_comment_tree(child, seen, depth + 1)] if reply and is_new]
    comment = _normalize_comment_candidate(node, replies=replies)
    if not comment:
        return None, False
    comment_id = str(comment.get("id") or "").strip()
    if comment_id and comment_id in seen:
        merged_comment = _merge_comment_items(seen[comment_id], comment)
        seen[comment_id].clear()
        seen[comment_id].update(merged_comment)
        return seen[comment_id], False
    if comment_id:
        seen[comment_id] = comment
    return comment, True


def _collect_comments_from_payload(node: object, out: list[dict], seen: dict[str, dict], depth: int = 0) -> None:
    if depth > 12 or node is None:
        return
    if isinstance(node, list):
        for item in node:
            _collect_comments_from_payload(item, out, seen, depth + 1)
        return
    if not isinstance(node, dict):
        return
    comment, is_new = _collect_comment_tree(node, seen, depth)
    if comment:
        if is_new:
            out.append(comment)
        child_values = {id(value) for key, value in node.items() if key in _COMMENT_CHILD_KEYS and isinstance(value, list)}
        for value in node.values():
            if id(value) not in child_values:
                _collect_comments_from_payload(value, out, seen, depth + 1)
        return
    for value in node.values():
        _collect_comments_from_payload(value, out, seen, depth + 1)


def normalize_threaded_comments(comment_items: list[dict] | None) -> list[dict]:
    roots: list[dict] = []
    roots_by_key: dict[str, dict] = {}
    pending: dict[str, list[dict]] = {}
    fallback = ""

    def prepare(item: dict) -> dict:
        if isinstance(item.get("author"), dict) and "createdAt" in item:
            return dict(item)
        return _normalize_comment_candidate(item) or dict(item)

    def remaining(key: str) -> int:
        root = roots_by_key.get(key)
        return max(_comment_reply_count(root) - len(_comment_reply_items(root)), 0) if root else 0

    def add_reply(key: str, reply: dict) -> None:
        nonlocal fallback
        root = roots_by_key.get(key)
        if not root:
            pending.setdefault(key, []).append(reply)
            return
        root["replies"] = _merge_threaded_comment_list(_comment_reply_items(root), [reply])
        root["replyCount"] = max(len(root["replies"]), _comment_reply_count(root))
        fallback = key if remaining(key) > 0 else ""

    def add_root(item: dict) -> None:
        nonlocal fallback
        key = _comment_identity_key(item)
        normalized = prepare(item)
        normalized["replies"] = _merge_threaded_comment_list([], _comment_reply_items(item))
        normalized["replyCount"] = max(len(normalized["replies"]), _comment_reply_count(normalized))
        if key in roots_by_key:
            merged_root = _merge_comment_items(roots_by_key[key], normalized)
            roots_by_key[key].clear()
            roots_by_key[key].update(merged_root)
        else:
            roots.append(normalized)
            roots_by_key[key] = normalized
        for reply in pending.pop(key, []):
            add_reply(key, reply)
        fallback = key if remaining(key) > 0 else ""

    for item in comment_items or []:
        if not isinstance(item, dict):
            continue
        normalized = prepare(item)
        normalized["replies"] = _merge_threaded_comment_list([], _comment_reply_items(item))
        normalized["replyCount"] = max(len(normalized["replies"]), _comment_reply_count(normalized))
        if _is_placeholder_comment_payload(normalized):
            continue
        key = _comment_identity_key(normalized)
        root_key = str(item.get("rootCommentId") or item.get("root_comment_id") or "").strip()
        if root_key and root_key != key:
            add_reply(root_key, normalized)
        elif fallback and fallback != key and remaining(fallback) > 0 and _looks_like_flat_reply_candidate(item):
            add_reply(fallback, normalized)
        else:
            add_root(normalized)
    for replies in pending.values():
        for reply in replies:
            if not _is_placeholder_comment_payload(reply):
                add_root(reply)
    return roots


def _iter_payload_dicts(node: object, depth: int = 0):
    if depth > 10 or node is None:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_payload_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_payload_dicts(item, depth + 1)


def extract_comment_replies(payload: object, root_comment_id: str) -> list[dict]:
    if not root_comment_id:
        return []
    direct: list[dict] = []
    direct_seen: dict[str, dict] = {}
    for node in _iter_payload_dicts(payload):
        for key in _COMMENT_CHILD_KEYS:
            for raw_reply in node.get(key) if isinstance(node.get(key), list) else []:
                reply, is_new = _collect_comment_tree(raw_reply, direct_seen)
                if reply and is_new and str(reply.get("id") or "").strip() != root_comment_id:
                    if not str(reply.get("rootCommentId") or reply.get("root_comment_id") or "").strip():
                        reply["rootCommentId"] = root_comment_id
                    direct = _merge_threaded_comment_list(direct, [reply])
    comments: list[dict] = []
    _collect_comments_from_payload(payload, comments, {})
    replies = _merge_threaded_comment_list([], direct)
    for item in normalize_threaded_comments(comments):
        item_id = str(item.get("id") or "").strip()
        item_root_id = str(item.get("rootCommentId") or item.get("root_comment_id") or "").strip()
        if item_id == root_comment_id:
            replies = _merge_threaded_comment_list(replies, _comment_reply_items(item))
        elif item_root_id == root_comment_id or not item_root_id and _looks_like_flat_reply_candidate(item):
            replies = _merge_threaded_comment_list(replies, [item])
    return replies


def extract_comment_list_items(payload: object) -> list[dict]:
    comments: list[dict] = []
    seen: dict[str, dict] = {}
    if isinstance(payload, dict) and isinstance(payload.get("hits"), list):
        for hit in payload["hits"]:
            if not isinstance(hit, dict):
                continue
            source = hit.get("comment") if isinstance(hit.get("comment"), dict) else hit.get("ratingItem") if isinstance(hit.get("ratingItem"), dict) else None
            if source is not None:
                comment, is_new = _collect_comment_tree(source, seen)
                if comment and is_new:
                    comments.append(comment)
            else:
                _collect_comments_from_payload(hit, comments, seen)
    else:
        _collect_comments_from_payload(payload, comments, seen)
    return normalize_threaded_comments(comments)


def _normalize_payload_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def extract_comment_sections(payload: object) -> list[object]:
    sections: list[object] = []
    seen: set[int] = set()

    def collect(node: object, depth: int = 0, parent_key: str = "") -> None:
        if depth > 10 or node is None:
            return
        if isinstance(node, list):
            if depth <= 2 or any(token in parent_key for token in _COMMENT_SECTION_KEYWORDS) or parent_key in _COMMENT_LIST_CONTAINER_KEYS:
                for item in node:
                    collect(item, depth + 1, parent_key)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            normalized_key = _normalize_payload_key(key)
            is_comment_key = any(token in normalized_key for token in _COMMENT_SECTION_KEYWORDS)
            if is_comment_key:
                if isinstance(value, (dict, list)) and id(value) not in seen:
                    seen.add(id(value))
                    sections.append(value)
                collect(value, depth + 1, normalized_key)
            elif depth <= 1 or normalized_key in _COMMENT_SEARCH_ROOT_KEYS or (any(token in parent_key for token in _COMMENT_SECTION_KEYWORDS) and normalized_key in _COMMENT_LIST_CONTAINER_KEYS):
                collect(value, depth + 1, normalized_key)

    collect(payload)
    return sections


def _extract_comment_count_from_payload(node: object, depth: int = 0, found: Optional[list[int]] = None) -> list[int]:
    found = found if found is not None else []
    if depth > 10 or node is None:
        return found
    if isinstance(node, list):
        for item in node:
            _extract_comment_count_from_payload(item, depth + 1, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            if _normalize_payload_key(key) in {"commentcount", "commentscount", "reviewcount", "commenttotal", "totalcomments"}:
                numeric = _comment_numeric(value)
                if 0 <= numeric <= 50000:
                    found.append(numeric)
            _extract_comment_count_from_payload(value, depth + 1, found)
    return found


def resolve_comment_count(*, unique_sections: list[object], next_data: dict, design: dict, comment_total: int, page_fetch_stats: dict[str, object]) -> int:
    api_total_known = bool((page_fetch_stats or {}).get("total_known"))
    api_total = _comment_numeric((page_fetch_stats or {}).get("total"))
    if api_total > 0 or api_total_known:
        return max(api_total, comment_total)
    counts = design.get("counts") if isinstance(design, dict) and isinstance(design.get("counts"), dict) else {}
    design_count = _comment_numeric(design.get("commentCount") if isinstance(design, dict) else 0) or _comment_numeric(design.get("commentsCount") if isinstance(design, dict) else 0) or _comment_numeric(design.get("reviewCount") if isinstance(design, dict) else 0) or _comment_numeric(counts.get("comments"))
    if design_count > 0:
        return max(design_count, comment_total)
    hints: list[int] = []
    for section in unique_sections or []:
        _extract_comment_count_from_payload(section, found=hints)
    if not hints:
        hints = _extract_comment_count_from_payload(next_data)
    return max(max(hints), comment_total) if hints else comment_total
