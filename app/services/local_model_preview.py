from __future__ import annotations

import html
import json
import math
import struct
from pathlib import Path
from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.legacy_archiver import sanitize_filename


MODEL_PREVIEW_SUFFIXES = {".stl"}
PREVIEW_REL_DIR = "images"
PREVIEW_KIND = "generated_stl_preview"
PREVIEW_VERSION = 1


def _is_stl_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MODEL_PREVIEW_SUFFIXES


def _iter_ascii_stl_vertices(path: Path, max_vertices: int) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if len(vertices) >= max_vertices:
                    break
                parts = line.strip().split()
                if len(parts) != 4 or parts[0].lower() != "vertex":
                    continue
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
    except OSError:
        return []
    return vertices


def _iter_binary_stl_vertices(path: Path, max_vertices: int) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    try:
        with path.open("rb") as handle:
            header = handle.read(84)
            if len(header) < 84:
                return []
            triangle_count = struct.unpack("<I", header[80:84])[0]
            limit = min(triangle_count, max(1, max_vertices // 3))
            for index in range(limit):
                triangle_index = min(triangle_count - 1, int(index * triangle_count / limit))
                handle.seek(84 + triangle_index * 50)
                chunk = handle.read(50)
                if len(chunk) < 50:
                    break
                values = struct.unpack("<12f", chunk[:48])
                vertices.extend(
                    [
                        (values[3], values[4], values[5]),
                        (values[6], values[7], values[8]),
                        (values[9], values[10], values[11]),
                    ]
                )
                if len(vertices) >= max_vertices:
                    break
    except (OSError, struct.error):
        return []
    return vertices


def _read_stl_vertices(path: Path, max_vertices: int = 24000) -> list[tuple[float, float, float]]:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(5)
    except OSError:
        return []
    if not prefix:
        return []

    vertices: list[tuple[float, float, float]] = []
    if prefix.lower() == b"solid":
        vertices = _iter_ascii_stl_vertices(path, max_vertices)
    if not vertices:
        vertices = _iter_binary_stl_vertices(path, max_vertices)
    return [point for point in vertices if all(math.isfinite(axis) for axis in point)]


def _project_points(vertices: list[tuple[float, float, float]]) -> dict[str, Any]:
    if not vertices:
        return {}

    xs = [point[0] for point in vertices]
    ys = [point[1] for point in vertices]
    zs = [point[2] for point in vertices]
    center = (
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        (min(zs) + max(zs)) / 2,
    )

    projected: list[tuple[float, float, float]] = []
    for x, y, z in vertices:
        dx = x - center[0]
        dy = y - center[1]
        dz = z - center[2]
        iso_x = (dx - dy) * 0.866
        iso_y = (dx + dy) * 0.28 - dz * 0.92
        projected.append((iso_x, iso_y, dz))

    px = [point[0] for point in projected]
    py = [point[1] for point in projected]
    pz = [point[2] for point in projected]
    width = max(px) - min(px)
    height = max(py) - min(py)
    span = max(width, height, 1.0)
    scale = 310 / span
    normalized = [
        (
            360 + (x - (min(px) + width / 2)) * scale,
            250 + (y - (min(py) + height / 2)) * scale,
            z,
        )
        for x, y, z in projected
    ]
    return {
        "points": normalized,
        "depth_min": min(pz) if pz else 0,
        "depth_max": max(pz) if pz else 1,
    }


def _sample_points(points: list[tuple[float, float, float]], limit: int = 900) -> list[tuple[float, float, float]]:
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    return points[::step][:limit]


def _preview_svg(title: str, vertices: list[tuple[float, float, float]]) -> str:
    clean_title = str(title or "本地 STL").strip() or "本地 STL"
    projection = _project_points(vertices)
    points = _sample_points(list(projection.get("points") or []))
    depth_min = float(projection.get("depth_min") or 0)
    depth_max = float(projection.get("depth_max") or 1)
    depth_span = depth_max - depth_min or 1

    if points:
        circles = []
        for x, y, z in sorted(points, key=lambda item: item[2]):
            tone = 150 + int(((z - depth_min) / depth_span) * 80)
            circles.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.1" fill="rgb({tone},{tone + 6},{tone + 12})" opacity="0.74"/>'
            )
        body = "\n".join(circles)
    else:
        body = (
            '<path d="M238 286 360 204 494 286 360 368Z" fill="#e5ecf2" stroke="#a7b2be" stroke-width="4"/>'
            '<path d="M238 286 360 368 360 482 238 398Z" fill="#cbd5df" stroke="#a7b2be" stroke-width="4"/>'
            '<path d="M360 368 494 286 494 402 360 482Z" fill="#dbe4ec" stroke="#a7b2be" stroke-width="4"/>'
        )

    escaped_title = html.escape(clean_title)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="720" height="540" viewBox="0 0 720 540">
<rect width="720" height="540" rx="28" fill="#f8fafc"/>
<rect x="24" y="24" width="672" height="492" rx="22" fill="#eef2f6" stroke="#d9e2ea" stroke-width="2"/>
<ellipse cx="360" cy="426" rx="205" ry="34" fill="#cfd8e2" opacity="0.66"/>
<g>{body}</g>
<text x="360" y="486" text-anchor="middle" font-family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="28" font-weight="760" fill="#111827">{escaped_title}</text>
<text x="360" y="516" text-anchor="middle" font-family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="16" font-weight="650" fill="#64748b">STL 自动预览</text>
</svg>
'''


def _preview_filename(source_path: Path) -> str:
    stem = sanitize_filename(source_path.stem).strip() or "model"
    return f"stl_preview_{stem}.svg"


def generate_stl_preview(source_path: Path, images_dir: Path, *, title: str = "") -> str:
    if not _is_stl_path(source_path):
        return ""
    vertices = _read_stl_vertices(source_path)
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / _preview_filename(source_path)
    target.write_text(_preview_svg(title or source_path.stem, vertices), encoding="utf-8")
    return f"{PREVIEW_REL_DIR}/{target.name}"


def ensure_package_preview_images(
    *,
    model_root: Path,
    model_files: list[dict[str, Any]],
    image_paths: list[str],
    title: str,
) -> list[str]:
    if image_paths:
        return image_paths
    images_dir = model_root / PREVIEW_REL_DIR
    for item in model_files:
        target_path = Path(str(item.get("target_path") or ""))
        rel_path = generate_stl_preview(target_path, images_dir, title=title)
        if rel_path:
            return [rel_path]
    return image_paths


def _meta_has_cover(meta: dict[str, Any]) -> bool:
    if str(meta.get("cover") or "").strip():
        return True
    for item in meta.get("designImages") or []:
        if isinstance(item, dict) and str(item.get("relPath") or item.get("localName") or item.get("url") or "").strip():
            return True
    return False


def ensure_local_model_preview(model_root: Path) -> bool:
    meta_path = model_root / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict) or str(meta.get("source") or "").strip().lower() != "local":
        return False
    if _meta_has_cover(meta):
        return False

    title = str(meta.get("title") or model_root.name)
    preview_rel = ""
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        file_name = Path(str(instance.get("fileName") or instance.get("name") or "")).name
        if not file_name:
            continue
        preview_rel = generate_stl_preview(model_root / "instances" / file_name, model_root / PREVIEW_REL_DIR, title=title)
        if preview_rel:
            break
    if not preview_rel:
        return False

    gallery_items = [{"relPath": preview_rel, "kind": PREVIEW_KIND, "generated": True}]
    meta["cover"] = preview_rel
    meta["designImages"] = gallery_items
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        if not str(instance.get("thumbnailLocal") or "").strip():
            instance["thumbnailLocal"] = preview_rel
        if not instance.get("pictures"):
            instance["pictures"] = gallery_items
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    local_import["previewGeneratedAt"] = china_now_iso()
    local_import["previewVersion"] = PREVIEW_VERSION
    meta["localImport"] = local_import
    meta["update_time"] = china_now_iso()

    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True
