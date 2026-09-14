import json
import math
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from app_paths import APP_ROOT
from model.MaterialSourceDownloader import (
    DownloadError,
    download_google_drive_source,
    parse_material_drive_link,
    resolve_google_drive_folder_name,
)

DEFAULT_INVENTORY_STATE_FILE = APP_ROOT / "InventoryState.json"
DEFAULT_MATERIAL_LIBRARY_ROOT = APP_ROOT / "MaterialLibrary"
SECONDS_PER_DAY = 24 * 60 * 60
PERSON_AVATAR_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MATERIAL_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".jfif",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".avif",
}
GOOGLE_SHEET_LINK_RE = re.compile(
    r"^https?://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)

STATUS_NORMAL = "normal"
STATUS_INITIAL = "initial"
STATUS_MODERATE = "moderate"
STATUS_CRITICAL = "critical"

STATUS_RANK = {
    STATUS_NORMAL: 0,
    STATUS_INITIAL: 1,
    STATUS_MODERATE: 2,
    STATUS_CRITICAL: 3,
}

STATUS_LABELS = {
    STATUS_NORMAL: "充足",
    STATUS_INITIAL: "初步报警",
    STATUS_MODERATE: "中度报警",
    STATUS_CRITICAL: "高危报警",
}


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return max(0.0, result)


def _integer(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(default)


def current_quantity(item, now=None):
    now = time.time() if now is None else float(now)
    quantity = _number(item.get("quantity"))
    daily_usage = _number(item.get("daily_usage"))
    updated_at = _number(item.get("updated_at"), now)
    elapsed = max(0.0, now - updated_at)
    return max(0.0, quantity - daily_usage * elapsed / SECONDS_PER_DAY)


def item_status(item, now=None):
    quantity = current_quantity(item, now)
    daily_usage = _number(item.get("daily_usage"))
    if quantity <= 1e-9:
        return STATUS_CRITICAL
    if daily_usage <= 1e-9:
        return STATUS_NORMAL
    days_left = quantity / daily_usage
    if days_left < 1:
        return STATUS_MODERATE
    if days_left < 2:
        return STATUS_INITIAL
    return STATUS_NORMAL


def inventory_summary(items, now=None):
    now = time.time() if now is None else float(now)
    counts = {status: 0 for status in STATUS_RANK}
    evaluated = []
    worst = STATUS_NORMAL
    for item in items:
        row = dict(item)
        row["current_quantity"] = current_quantity(item, now)
        daily_usage = _number(item.get("daily_usage"))
        row["days_left"] = (
            row["current_quantity"] / daily_usage
            if daily_usage > 1e-9
            else math.inf
        )
        row["status"] = item_status(item, now)
        counts[row["status"]] += 1
        if STATUS_RANK[row["status"]] > STATUS_RANK[worst]:
            worst = row["status"]
        evaluated.append(row)
    return {"items": evaluated, "counts": counts, "worst": worst}


class InventoryStore:
    def __init__(self, path=None, material_root=None):
        self.path = Path(path or DEFAULT_INVENTORY_STATE_FILE)
        if material_root is None:
            material_root = (
                DEFAULT_MATERIAL_LIBRARY_ROOT
                if path is None
                else self.path.parent / "MaterialLibrary"
            )
        self.material_root = Path(material_root)

    def _default_state(self):
        return {
            "version": 4,
            "items": [],
            "materials": [],
            "people": [],
            "material_library_root": str(self.material_root.resolve()),
        }

    def load(self):
        if not self.path.exists():
            return self._default_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("库存记录无法读取：{}".format(error)) from error
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("库存记录格式不正确")
        items = []
        now = time.time()
        for raw in data.get("items", []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            items.append(
                {
                    "id": str(raw.get("id") or uuid.uuid4().hex),
                    "name": name,
                    "quantity": _number(raw.get("quantity")),
                    "daily_usage": _number(raw.get("daily_usage")),
                    "updated_at": _number(raw.get("updated_at"), now),
                }
            )
        materials = []
        raw_materials = data.get("materials", [])
        if isinstance(raw_materials, list):
            for raw in raw_materials:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name", "")).strip()
                path = str(raw.get("path", "")).strip()
                if not name or not path:
                    continue
                sources = []
                for source in raw.get("sources", []):
                    if not isinstance(source, dict):
                        continue
                    source_type = str(source.get("type", "")).strip()
                    value = str(source.get("value", "")).strip()
                    if source_type not in {
                        "local",
                        "google_drive",
                        "material_library",
                    } or not value:
                        continue
                    normalized_source = dict(source)
                    normalized_source.update({
                        "type": source_type,
                        "value": value,
                        "label": str(source.get("label", "")).strip(),
                    })
                    sources.append(normalized_source)
                source_summary = str(raw.get("source_summary", "")).strip()
                if not source_summary:
                    source_summary = f"{_integer(raw.get('source_count', 0))} 项"
                materials.append({
                    "id": str(raw.get("id") or uuid.uuid4().hex),
                    "name": name,
                    "path": path,
                    "created_at": str(raw.get("created_at", "")).strip(),
                    "updated_at": str(
                        raw.get("updated_at") or raw.get("created_at", "")
                    ).strip(),
                    "source_count": _integer(raw.get("source_count", 0)),
                    "source_summary": source_summary,
                    "sources": sources,
                })
        people = []
        raw_people = data.get("people", [])
        if isinstance(raw_people, list):
            for raw in raw_people:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name", "")).strip()
                path = str(raw.get("path", "")).strip()
                if not name or not path:
                    continue
                sources = []
                for source in raw.get("sources", []):
                    if not isinstance(source, dict):
                        continue
                    source_type = str(source.get("type", "")).strip()
                    value = str(source.get("value", "")).strip()
                    if source_type not in {
                        "local",
                        "google_drive",
                        "material_library",
                    } or not value:
                        continue
                    normalized_source = dict(source)
                    normalized_source.update({
                        "type": source_type,
                        "value": value,
                        "label": str(source.get("label", "")).strip(),
                    })
                    sources.append(normalized_source)
                bindings = raw.get("bindings", {})
                bindings = dict(bindings) if isinstance(bindings, dict) else {}
                bindings["google_sheets"] = self._normalize_google_sheet_links(
                    bindings.get("google_sheets", raw.get("google_sheet_links", [])),
                    strict=False,
                )
                metadata = raw.get("metadata", {})
                metadata = dict(metadata) if isinstance(metadata, dict) else {}
                person_path = Path(path)
                people.append({
                    "id": str(raw.get("id") or uuid.uuid4().hex),
                    "name": name,
                    "path": path,
                    "material_path": str(
                        raw.get("material_path") or person_path / "素材"
                    ),
                    "avatar_path": str(raw.get("avatar_path", "")).strip(),
                    "bindings": bindings,
                    "metadata": metadata,
                    "created_at": str(raw.get("created_at", "")).strip(),
                    "updated_at": str(
                        raw.get("updated_at") or raw.get("created_at", "")
                    ).strip(),
                    "source_count": len(sources),
                    "source_summary": self._material_source_summary(sources),
                    "sources": sources,
                })
        return {
            "version": 4,
            "items": items,
            "materials": materials,
            "people": people,
            "material_library_root": str(self.material_root.resolve()),
        }

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temp_path), str(self.path))
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def list_items(self):
        return self.load()["items"]

    def list_materials(self):
        return self.load()["materials"]

    @staticmethod
    def _images_for_source(source_kind, source, root_text, seen_paths):
        images = []
        root_text = str(root_text or "").strip()
        if not root_text:
            return images
        root = Path(root_text)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            try:
                candidates = sorted(
                    (
                        path
                        for path in root.rglob("*")
                        if path.is_file()
                        and path.suffix.lower() in MATERIAL_IMAGE_SUFFIXES
                    ),
                    key=lambda path: str(path.relative_to(root)).casefold(),
                )
            except OSError:
                candidates = []
        else:
            candidates = []

        source_id = str(source.get("id") or "")
        source_name = str(source.get("name") or "")
        for image_path in candidates:
            if image_path.suffix.lower() not in MATERIAL_IMAGE_SUFFIXES:
                continue
            try:
                resolved = image_path.resolve()
                size_bytes = image_path.stat().st_size
            except OSError:
                continue
            path_key = os.path.normcase(str(resolved))
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            try:
                relative_path = str(image_path.relative_to(root))
            except ValueError:
                relative_path = image_path.name
            images.append({
                "id": f"{source_kind}:{source_id}:{relative_path}",
                "source_kind": source_kind,
                "source_id": source_id,
                "source_name": source_name,
                # Keep the old names for callers written before people sources
                # were included in task image assignment.
                "material_id": source_id,
                "material_name": source_name,
                "name": image_path.name,
                "relative_path": relative_path,
                "path": str(resolved),
                "size_bytes": size_bytes,
            })
        return images

    @classmethod
    def _image_groups_from_state(cls, state):
        groups = []
        seen_paths = set()
        for material in state.get("materials", []):
            root_text = str(material.get("path") or "").strip()
            images = cls._images_for_source(
                "material",
                material,
                root_text,
                seen_paths,
            )
            groups.append({
                "id": f"material:{material.get('id', '')}",
                "source_kind": "material",
                "source_id": str(material.get("id") or ""),
                "source_type_label": "素材管理",
                "name": str(material.get("name") or ""),
                "path": root_text,
                "image_count": len(images),
                "images": images,
            })
        for person in state.get("people", []):
            root_text = str(person.get("material_path") or "").strip()
            if not root_text and str(person.get("path") or "").strip():
                root_text = str(Path(person["path"]) / "素材")
            images = cls._images_for_source(
                "person",
                person,
                root_text,
                seen_paths,
            )
            groups.append({
                "id": f"person:{person.get('id', '')}",
                "source_kind": "person",
                "source_id": str(person.get("id") or ""),
                "source_type_label": "人物素材",
                "name": str(person.get("name") or ""),
                "path": root_text,
                "image_count": len(images),
                "images": images,
            })
        return groups

    @classmethod
    def _material_images_from_state(cls, state):
        return [
            image
            for group in cls._image_groups_from_state(state)
            if group["source_kind"] == "material"
            for image in group["images"]
        ]

    def list_material_images(self):
        """List assignable image files in the regular material library."""
        return self._material_images_from_state(self.load())

    def list_image_groups(self):
        """List regular and person material entries with their contained images."""
        return self._image_groups_from_state(self.load())

    def move_material_images(self, assignments):
        """Move material images to target directories as one rollback-safe batch."""
        state = self.load()
        available = {
            os.path.normcase(str(Path(image["path"]).resolve())): image
            for group in self._image_groups_from_state(state)
            for image in group["images"]
        }
        normalized = []
        seen_sources = set()
        material_root = self.material_root.resolve()
        for raw_assignment in assignments or ():
            if not isinstance(raw_assignment, dict):
                raise ValueError("图片分配数据无效")
            source_text = str(raw_assignment.get("path") or "").strip()
            target_text = str(raw_assignment.get("target_dir") or "").strip()
            if not source_text or not target_text:
                raise ValueError("图片来源或目标任务目录为空")
            source = Path(source_text).resolve()
            source_key = os.path.normcase(str(source))
            image = available.get(source_key)
            if image is None or not source.is_file():
                raise ValueError(f"图片已经不在素材库中：{source}")
            if source_key in seen_sources:
                raise ValueError(f"同一张图片不能重复分配：{source.name}")
            target_dir = Path(target_text).resolve()
            try:
                target_dir.relative_to(material_root)
            except ValueError:
                pass
            else:
                raise ValueError("任务目录不能位于素材库内部")
            seen_sources.add(source_key)
            normalized.append({
                "source": source,
                "target_dir": target_dir,
                "image": image,
                "task_label": str(raw_assignment.get("task_label") or ""),
            })
        if not normalized:
            raise ValueError("请至少选择一张要分配的图片")

        moved = []
        try:
            for assignment in normalized:
                target_dir = assignment["target_dir"]
                target_dir.mkdir(parents=True, exist_ok=True)
                target = self._unique_copy_target(
                    target_dir,
                    assignment["source"].name,
                )
                shutil.move(str(assignment["source"]), str(target))
                moved.append({
                    "source": assignment["source"],
                    "target": target,
                    "image": assignment["image"],
                    "task_label": assignment["task_label"],
                })

            removed_materials = [
                material
                for material in state.get("materials", [])
                if not str(material.get("path") or "").strip()
                or not material_directory_has_files(material.get("path"))
            ]
            if removed_materials:
                removed_ids = {
                    str(material.get("id") or "")
                    for material in removed_materials
                }
                state["materials"] = [
                    material
                    for material in state.get("materials", [])
                    if str(material.get("id") or "") not in removed_ids
                ]
                state["version"] = max(4, _integer(state.get("version"), 4))
                self._write(state)
        except Exception:
            for item in reversed(moved):
                target = item["target"]
                source = item["source"]
                try:
                    if target.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(source))
                except OSError:
                    pass
            raise

        for material in removed_materials:
            path = Path(str(material.get("path") or ""))
            if path.is_dir() and not material_directory_has_files(path):
                shutil.rmtree(path, ignore_errors=True)
        return {
            "moved": [
                {
                    "source": str(item["source"]),
                    "target": str(item["target"]),
                    "material_id": item["image"]["material_id"],
                    "material_name": item["image"]["material_name"],
                    "source_kind": item["image"]["source_kind"],
                    "task_label": item["task_label"],
                }
                for item in moved
            ],
            "removed_materials": removed_materials,
        }

    def list_people(self):
        return self.load()["people"]

    def _settle(self, items, now):
        settled = []
        for item in items:
            row = dict(item)
            row["quantity"] = current_quantity(item, now)
            row["updated_at"] = now
            settled.append(row)
        return settled

    def add_item(self, name, quantity, daily_usage):
        name = str(name or "").strip()
        if not name:
            raise ValueError("库存名称不能为空")
        state = self.load()
        if any(item["name"].casefold() == name.casefold() for item in state["items"]):
            raise ValueError("已经有同名库存：{}".format(name))
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        item = {
            "id": uuid.uuid4().hex,
            "name": name,
            "quantity": _number(quantity),
            "daily_usage": _number(daily_usage),
            "updated_at": now,
        }
        state["items"].append(item)
        self._write(state)
        return item

    def update_item(self, item_id, name, quantity, daily_usage):
        name = str(name or "").strip()
        if not name:
            raise ValueError("库存名称不能为空")
        state = self.load()
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        if any(
            item["id"] != item_id and item["name"].casefold() == name.casefold()
            for item in state["items"]
        ):
            raise ValueError("已经有同名库存：{}".format(name))
        for item in state["items"]:
            if item["id"] == item_id:
                item.update(
                    name=name,
                    quantity=_number(quantity),
                    daily_usage=_number(daily_usage),
                    updated_at=now,
                )
                self._write(state)
                return item
        raise KeyError("找不到要修改的库存")

    def add_stock(self, item_id, amount):
        amount = _number(amount)
        if amount <= 0:
            raise ValueError("补充数量必须大于 0")
        state = self.load()
        now = time.time()
        state["items"] = self._settle(state["items"], now)
        for item in state["items"]:
            if item["id"] == item_id:
                item["quantity"] += amount
                self._write(state)
                return item
        raise KeyError("找不到要补充的库存")

    def delete_item(self, item_id):
        state = self.load()
        before = len(state["items"])
        state["items"] = [item for item in state["items"] if item["id"] != item_id]
        if len(state["items"]) == before:
            raise KeyError("找不到要删除的库存")
        self._write(state)

    def summary(self, now=None):
        return inventory_summary(self.list_items(), now)

    @staticmethod
    def _safe_material_directory_name(name):
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", str(name or "")).strip(" .")
        return safe_name[:80] or "素材"

    @staticmethod
    def _unique_copy_target(parent, source_name):
        source_name = str(source_name or "素材")
        candidate = parent / source_name
        if not candidate.exists():
            return candidate
        source_path = Path(source_name)
        stem = source_path.stem or "素材"
        suffix = source_path.suffix
        index = 2
        while True:
            candidate = parent / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def _normalize_material_sources(self, source_paths):
        sources = []
        seen = set()
        material_root = self.material_root.resolve()
        for raw_path in source_paths or ():
            text = str(raw_path or "").strip().strip('"')
            if not text:
                continue
            if text.casefold().startswith(("http://", "https://")):
                try:
                    drive_link = parse_material_drive_link(text)
                except DownloadError as error:
                    raise ValueError(f"不支持的素材链接：{error}") from error
                key = f"google_drive:{drive_link.identity}"
                if key not in seen:
                    seen.add(key)
                    sources.append({
                        "type": "google_drive",
                        "value": text,
                        "label": (
                            "Google Drive 文件夹"
                            if drive_link.is_folder
                            else "Google Drive 文件"
                        ),
                    })
                continue
            source = Path(text).expanduser()
            if not source.exists():
                raise ValueError(f"素材来源不存在：{source}")
            source = source.resolve()
            if source == material_root or material_root in source.parents:
                raise ValueError("不能把素材库自身或其内部目录再次加入素材库")
            key = os.path.normcase(str(source))
            if key not in seen:
                seen.add(key)
                sources.append({
                    "type": "local",
                    "value": str(source),
                    "label": source.name,
                })
        if not sources:
            raise ValueError("请添加至少一个本地文件、文件夹或 Google Drive 链接")
        return sources

    @staticmethod
    def _material_source_summary(sources):
        local_count = sum(item.get("type") == "local" for item in sources)
        drive_count = sum(item.get("type") == "google_drive" for item in sources)
        library_count = sum(
            item.get("type") == "material_library" for item in sources
        )
        known_count = local_count + drive_count + library_count
        other_count = max(0, len(sources) - known_count)
        parts = []
        if local_count:
            parts.append(f"本地 {local_count}")
        if drive_count:
            parts.append(f"网盘 {drive_count}")
        if library_count:
            parts.append(f"素材库 {library_count}")
        if other_count:
            parts.append(f"其他 {other_count}")
        return " / ".join(parts)

    @property
    def people_root(self):
        return self.material_root / "人物素材"

    @staticmethod
    def _normalize_google_sheet_links(values, strict=True):
        if isinstance(values, str):
            values = re.split(r"[\s,，;；]+", values)
        elif not isinstance(values, (list, tuple, set)):
            values = [] if values is None else [values]
        links = []
        seen_keys = set()
        for value in values:
            link = str(value or "").strip()
            if not link:
                continue
            match = GOOGLE_SHEET_LINK_RE.fullmatch(link)
            if not match:
                if strict:
                    raise ValueError(f"不是有效的 Google 表格链接：{link}")
                continue
            spreadsheet_id = match.group(1)
            gid_match = re.search(r"(?:[?#&]gid=)(\d+)", link, flags=re.IGNORECASE)
            link_key = (spreadsheet_id, gid_match.group(1) if gid_match else "")
            if link_key in seen_keys:
                continue
            seen_keys.add(link_key)
            links.append(link)
        return links

    @staticmethod
    def _normalize_avatar_source(value):
        text = str(value or "").strip().strip('"')
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_file():
            raise ValueError(f"人物图片不存在：{path}")
        if path.suffix.lower() not in PERSON_AVATAR_SUFFIXES:
            raise ValueError("人物图片仅支持 PNG、JPG、WEBP、BMP 或 GIF")
        return path.resolve()

    def _copy_material_sources(self, sources, staging_dir, progress_callback=None):
        for index, source_info in enumerate(sources, 1):
            if progress_callback is not None:
                progress_callback(
                    f"正在处理来源 {index}/{len(sources)}：{source_info['label']}"
                )
            if source_info["type"] == "local":
                source = Path(source_info["value"])
                target = self._unique_copy_target(staging_dir, source.name)
                if source.is_dir():
                    shutil.copytree(str(source), str(target))
                else:
                    shutil.copy2(str(source), str(target))
                continue

            download_dir = staging_dir / f".drive-source-{index}"
            download_dir.mkdir()
            download_google_drive_source(
                source_info["value"],
                download_dir,
                progress_callback=progress_callback,
            )
            for downloaded in list(download_dir.iterdir()):
                target = self._unique_copy_target(staging_dir, downloaded.name)
                shutil.move(str(downloaded), str(target))
            download_dir.rmdir()

    @staticmethod
    def _remove_material_path(path):
        path = Path(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return
        try:
            path.unlink()
        except OSError:
            pass

    def add_material(
        self,
        name,
        source_paths,
        progress_callback=None,
        use_drive_folder_name=False,
    ):
        name = str(name or "").strip()
        if not name and not use_drive_folder_name:
            raise ValueError("素材名称不能为空")
        sources = self._normalize_material_sources(source_paths)
        if use_drive_folder_name:
            folder_source = next(
                (
                    item for item in sources
                    if item.get("type") == "google_drive"
                    and parse_material_drive_link(item.get("value", "")).is_folder
                ),
                None,
            )
            if folder_source is None:
                raise ValueError(
                    "已勾选“使用谷歌文件夹名称”，"
                    "但素材来源中没有 Google Drive 文件夹链接"
                )
            if progress_callback is not None:
                progress_callback("正在读取 Google Drive 文件夹名称…")
            try:
                name = resolve_google_drive_folder_name(folder_source["value"])
            except DownloadError as error:
                raise ValueError(str(error)) from error
        state = self.load()
        if any(
            item["name"].casefold() == name.casefold()
            for item in state["materials"]
        ):
            raise ValueError(f"已经有同名素材：{name}")

        material_id = uuid.uuid4().hex
        self.material_root.mkdir(parents=True, exist_ok=True)
        staging_dir = self.material_root / f".{material_id}.tmp"
        final_dir = self.material_root / (
            f"{self._safe_material_directory_name(name)}-{material_id[:8]}"
        )
        try:
            staging_dir.mkdir()
            self._copy_material_sources(
                sources,
                staging_dir,
                progress_callback=progress_callback,
            )
            if not material_directory_has_files(staging_dir):
                raise ValueError("拖入的文件夹中没有可保存的文件")
            os.replace(str(staging_dir), str(final_dir))
            created_at = datetime.now().astimezone().isoformat(timespec="seconds")
            material = {
                "id": material_id,
                "name": name,
                "path": str(final_dir.resolve()),
                "created_at": created_at,
                "updated_at": created_at,
                "source_count": len(sources),
                "source_summary": self._material_source_summary(sources),
                "sources": sources,
            }
            state["materials"].append(material)
            state["version"] = 4
            state["material_library_root"] = str(self.material_root.resolve())
            try:
                self._write(state)
            except OSError:
                shutil.rmtree(final_dir, ignore_errors=True)
                raise
            return material
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def append_material(self, material_id, source_paths, progress_callback=None):
        state = self.load()
        material = next(
            (item for item in state["materials"] if item["id"] == material_id),
            None,
        )
        if material is None:
            raise KeyError("找不到要追加的素材")

        final_dir = Path(material["path"])
        if not final_dir.exists():
            raise ValueError("素材保存位置已经不存在，请先点击“查库”清理记录")
        if not final_dir.is_dir():
            raise ValueError("素材保存位置不是文件夹，无法追加内容")

        sources = self._normalize_material_sources(source_paths)
        self.material_root.mkdir(parents=True, exist_ok=True)
        staging_dir = self.material_root / (
            f".{material_id}.append-{uuid.uuid4().hex[:8]}.tmp"
        )
        moved_targets = []
        try:
            staging_dir.mkdir()
            self._copy_material_sources(
                sources,
                staging_dir,
                progress_callback=progress_callback,
            )
            if not material_directory_has_files(staging_dir):
                raise ValueError("追加的文件夹中没有可保存的文件")

            for staged_item in list(staging_dir.iterdir()):
                target = self._unique_copy_target(final_dir, staged_item.name)
                shutil.move(str(staged_item), str(target))
                moved_targets.append(target)
            staging_dir.rmdir()

            material["sources"] = list(material.get("sources", [])) + sources
            material["source_count"] = len(material["sources"])
            material["source_summary"] = self._material_source_summary(
                material["sources"]
            )
            material["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            state["version"] = 4
            state["material_library_root"] = str(self.material_root.resolve())
            self._write(state)
            return material
        except Exception:
            for target in reversed(moved_targets):
                self._remove_material_path(target)
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def add_person(
        self,
        name,
        avatar_path="",
        google_sheet_links=None,
        source_paths=None,
        progress_callback=None,
        metadata=None,
        bindings=None,
    ):
        name = str(name or "").strip()
        if not name:
            raise ValueError("人物名称不能为空")
        state = self.load()
        if any(item["name"].casefold() == name.casefold() for item in state["people"]):
            raise ValueError(f"已经有同名人物：{name}")

        avatar_source = self._normalize_avatar_source(avatar_path)
        sheet_links = self._normalize_google_sheet_links(google_sheet_links)
        raw_sources = [value for value in (source_paths or []) if str(value or "").strip()]
        sources = self._normalize_material_sources(raw_sources) if raw_sources else []
        person_bindings = dict(bindings) if isinstance(bindings, dict) else {}
        person_bindings["google_sheets"] = sheet_links
        person_metadata = dict(metadata) if isinstance(metadata, dict) else {}

        person_id = uuid.uuid4().hex
        self.people_root.mkdir(parents=True, exist_ok=True)
        staging_dir = self.people_root / f".{person_id}.tmp"
        final_dir = self.people_root / (
            f"{self._safe_material_directory_name(name)}-{person_id[:8]}"
        )
        try:
            staging_dir.mkdir()
            staging_material_dir = staging_dir / "素材"
            staging_material_dir.mkdir()
            avatar_name = ""
            if avatar_source is not None:
                avatar_name = f"人物图片{avatar_source.suffix.lower()}"
                shutil.copy2(str(avatar_source), str(staging_dir / avatar_name))
            if sources:
                self._copy_material_sources(
                    sources,
                    staging_material_dir,
                    progress_callback=progress_callback,
                )
                if not material_directory_has_files(staging_material_dir):
                    raise ValueError("导入的人物素材中没有可保存的文件")

            os.replace(str(staging_dir), str(final_dir))
            now_text = datetime.now().astimezone().isoformat(timespec="seconds")
            person = {
                "id": person_id,
                "name": name,
                "path": str(final_dir.resolve()),
                "material_path": str((final_dir / "素材").resolve()),
                "avatar_path": (
                    str((final_dir / avatar_name).resolve()) if avatar_name else ""
                ),
                "bindings": person_bindings,
                "metadata": person_metadata,
                "created_at": now_text,
                "updated_at": now_text,
                "source_count": len(sources),
                "source_summary": self._material_source_summary(sources),
                "sources": sources,
            }
            state["people"].append(person)
            state["version"] = 4
            state["material_library_root"] = str(self.material_root.resolve())
            try:
                self._write(state)
            except OSError:
                shutil.rmtree(final_dir, ignore_errors=True)
                raise
            return person
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def append_person_material(self, person_id, source_paths, progress_callback=None):
        state = self.load()
        person = next(
            (item for item in state["people"] if item["id"] == person_id),
            None,
        )
        if person is None:
            raise KeyError("找不到要追加素材的人物")
        person_dir = Path(person["path"])
        if not person_dir.is_dir():
            raise ValueError("人物素材保存位置已经不存在，请先点击“查库”")

        sources = self._normalize_material_sources(source_paths)
        material_dir = Path(person["material_path"])
        material_dir_created = not material_dir.exists()
        material_dir.mkdir(parents=True, exist_ok=True)
        self.people_root.mkdir(parents=True, exist_ok=True)
        staging_dir = self.people_root / (
            f".{person_id}.append-{uuid.uuid4().hex[:8]}.tmp"
        )
        moved_targets = []
        try:
            staging_dir.mkdir()
            self._copy_material_sources(
                sources,
                staging_dir,
                progress_callback=progress_callback,
            )
            if not material_directory_has_files(staging_dir):
                raise ValueError("追加的人物素材中没有可保存的文件")
            for staged_item in list(staging_dir.iterdir()):
                target = self._unique_copy_target(material_dir, staged_item.name)
                shutil.move(str(staged_item), str(target))
                moved_targets.append(target)
            staging_dir.rmdir()

            person["sources"] = list(person.get("sources", [])) + sources
            person["source_count"] = len(person["sources"])
            person["source_summary"] = self._material_source_summary(person["sources"])
            person["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            state["version"] = 4
            self._write(state)
            return person
        except Exception:
            for target in reversed(moved_targets):
                self._remove_material_path(target)
            shutil.rmtree(staging_dir, ignore_errors=True)
            if material_dir_created:
                try:
                    material_dir.rmdir()
                except OSError:
                    pass
            raise

    def import_materials_to_person(
        self,
        person_id,
        material_ids,
        remove_originals=False,
        progress_callback=None,
    ):
        """Copy existing material-library entries into one person's material folder.

        The regular material directories are moved to a temporary holding directory
        before a destructive import is committed.  This lets both the copied person
        files and the original library entries be restored if the state write fails.
        """
        state = self.load()
        person = next(
            (item for item in state["people"] if item["id"] == person_id),
            None,
        )
        if person is None:
            raise KeyError("找不到要导入素材的人物")

        requested_ids = []
        seen_ids = set()
        for material_id in material_ids or ():
            material_id = str(material_id or "").strip()
            if material_id and material_id not in seen_ids:
                seen_ids.add(material_id)
                requested_ids.append(material_id)
        if not requested_ids:
            raise ValueError("请至少选择一项素材库素材")

        materials_by_id = {
            item["id"]: item for item in state["materials"]
        }
        missing_ids = [
            material_id
            for material_id in requested_ids
            if material_id not in materials_by_id
        ]
        if missing_ids:
            raise KeyError("部分素材记录已经不存在，请刷新素材库后重试")
        selected_materials = [
            materials_by_id[material_id] for material_id in requested_ids
        ]

        person_dir = Path(person["path"])
        if not person_dir.is_dir():
            raise ValueError("人物素材保存位置已经不存在，请先点击“查库”")
        source_paths = []
        seen_paths = set()
        for material in selected_materials:
            source_path = Path(material["path"])
            if not source_path.exists():
                raise ValueError(
                    f"素材“{material['name']}”的保存位置已经不存在，请先查库"
                )
            if not material_directory_has_files(source_path):
                raise ValueError(
                    f"素材“{material['name']}”的保存位置为空，请先查库"
                )
            normalized_path = os.path.normcase(str(source_path.resolve()))
            if normalized_path in seen_paths:
                raise ValueError("选中的素材记录指向同一目录，请先查库清理重复记录")
            seen_paths.add(normalized_path)
            source_paths.append(source_path)

        material_dir = Path(person["material_path"])
        material_dir_created = not material_dir.exists()
        material_dir.mkdir(parents=True, exist_ok=True)
        self.people_root.mkdir(parents=True, exist_ok=True)
        import_token = uuid.uuid4().hex[:8]
        staging_dir = self.people_root / (
            f".{person_id}.material-import-{import_token}.tmp"
        )
        holding_dir = self.material_root / (
            f".material-import-trash-{import_token}.tmp"
        )
        moved_targets = []
        held_paths = []

        try:
            staging_dir.mkdir()
            total = len(selected_materials)
            for index, (material, source_path) in enumerate(
                zip(selected_materials, source_paths),
                start=1,
            ):
                if progress_callback:
                    progress_callback(
                        f"正在导入素材库项目（{index}/{total}）：{material['name']}"
                    )
                group_name = self._safe_material_directory_name(material["name"])
                staged_group = self._unique_copy_target(staging_dir, group_name)
                if source_path.is_dir():
                    shutil.copytree(str(source_path), str(staged_group))
                else:
                    staged_group.mkdir()
                    shutil.copy2(
                        str(source_path),
                        str(staged_group / source_path.name),
                    )
                if not material_directory_has_files(staged_group):
                    raise ValueError(f"素材“{material['name']}”中没有可导入的文件")

            for staged_item in list(staging_dir.iterdir()):
                target = self._unique_copy_target(material_dir, staged_item.name)
                shutil.move(str(staged_item), str(target))
                moved_targets.append(target)
            staging_dir.rmdir()

            imported_sources = [
                {
                    "type": "material_library",
                    "value": material["id"],
                    "label": material["name"],
                    "original_path": str(source_path.resolve()),
                    "removed_original": bool(remove_originals),
                }
                for material, source_path in zip(selected_materials, source_paths)
            ]
            person["sources"] = list(person.get("sources", [])) + imported_sources
            person["source_count"] = len(person["sources"])
            person["source_summary"] = self._material_source_summary(person["sources"])
            person["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )

            if remove_originals:
                holding_dir.mkdir()
                for material, source_path in zip(selected_materials, source_paths):
                    held_path = self._unique_copy_target(
                        holding_dir,
                        source_path.name,
                    )
                    shutil.move(str(source_path), str(held_path))
                    held_paths.append((source_path, held_path))
                state["materials"] = [
                    item
                    for item in state["materials"]
                    if item["id"] not in seen_ids
                ]

            state["version"] = 4
            state["material_library_root"] = str(self.material_root.resolve())
            self._write(state)
        except Exception:
            for original_path, held_path in reversed(held_paths):
                if held_path.exists():
                    original_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(held_path), str(original_path))
            for target in reversed(moved_targets):
                self._remove_material_path(target)
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(holding_dir, ignore_errors=True)
            if material_dir_created:
                try:
                    material_dir.rmdir()
                except OSError:
                    pass
            raise

        shutil.rmtree(holding_dir, ignore_errors=True)
        if progress_callback:
            progress_callback(
                f"已向人物“{person['name']}”导入 {len(selected_materials)} 项素材"
            )
        return {
            "person": person,
            "imported_materials": selected_materials,
            "removed_materials": selected_materials if remove_originals else [],
        }

    def update_person_profile(
        self,
        person_id,
        name,
        google_sheet_links=None,
        avatar_path="",
        remove_avatar=False,
        metadata_update=None,
    ):
        name = str(name or "").strip()
        if not name:
            raise ValueError("人物名称不能为空")
        state = self.load()
        person = next(
            (item for item in state["people"] if item["id"] == person_id),
            None,
        )
        if person is None:
            raise KeyError("找不到要修改的人物")
        if any(
            item["id"] != person_id and item["name"].casefold() == name.casefold()
            for item in state["people"]
        ):
            raise ValueError(f"已经有同名人物：{name}")

        person_dir = Path(person["path"])
        if not person_dir.is_dir():
            raise ValueError("人物素材保存位置已经不存在，请先点击“查库”")
        sheet_links = self._normalize_google_sheet_links(google_sheet_links)
        avatar_source = self._normalize_avatar_source(avatar_path)
        old_avatar_text = str(person.get("avatar_path") or "").strip()
        old_avatar = Path(old_avatar_text) if old_avatar_text else None
        new_avatar_target = None
        avatar_temp = None
        avatar_backup = None
        avatar_installed = False

        try:
            if avatar_source is not None:
                same_avatar = (
                    old_avatar is not None
                    and old_avatar.exists()
                    and avatar_source == old_avatar.resolve()
                )
                if same_avatar:
                    new_avatar_target = old_avatar
                else:
                    new_avatar_target = person_dir / (
                        f"人物图片{avatar_source.suffix.lower()}"
                    )
                    avatar_temp = person_dir / (
                        f".avatar-{uuid.uuid4().hex[:8]}.tmp{avatar_source.suffix.lower()}"
                    )
                    shutil.copy2(str(avatar_source), str(avatar_temp))
                    if new_avatar_target.exists():
                        avatar_backup = person_dir / (
                            f".avatar-backup-{uuid.uuid4().hex[:8]}"
                            f"{new_avatar_target.suffix}"
                        )
                        os.replace(str(new_avatar_target), str(avatar_backup))
                    os.replace(str(avatar_temp), str(new_avatar_target))
                    avatar_installed = True

            person["name"] = name
            bindings = dict(person.get("bindings", {}))
            bindings["google_sheets"] = sheet_links
            person["bindings"] = bindings
            metadata = dict(person.get("metadata", {}))
            if isinstance(metadata_update, dict):
                metadata.update(metadata_update)
            person["metadata"] = metadata
            if new_avatar_target is not None:
                person["avatar_path"] = str(new_avatar_target.resolve())
            elif remove_avatar:
                person["avatar_path"] = ""
            person["updated_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            state["version"] = 4
            self._write(state)
        except Exception:
            if avatar_temp is not None:
                self._remove_material_path(avatar_temp)
            if avatar_installed and new_avatar_target is not None:
                self._remove_material_path(new_avatar_target)
            if avatar_backup is not None and avatar_backup.exists():
                os.replace(str(avatar_backup), str(new_avatar_target))
            raise

        if avatar_backup is not None:
            self._remove_material_path(avatar_backup)
        current_avatar = str(person.get("avatar_path") or "").strip()
        if old_avatar is not None and old_avatar.exists():
            if not current_avatar or Path(current_avatar) != old_avatar:
                self._remove_material_path(old_avatar)
        return person

    def remove_person_record(self, person_id):
        state = self.load()
        before = len(state["people"])
        state["people"] = [
            item for item in state["people"] if item["id"] != person_id
        ]
        if len(state["people"]) == before:
            raise KeyError("找不到要移除的人物")
        state["version"] = 4
        self._write(state)

    def check_people(self):
        state = self.load()
        kept = []
        removed = []
        cleared_avatars = []
        changed = False
        for person in state["people"]:
            if not Path(person["path"]).is_dir():
                removed.append({**person, "check_reason": "人物素材目录不存在"})
                changed = True
                continue
            avatar_path = str(person.get("avatar_path") or "").strip()
            if avatar_path and not Path(avatar_path).is_file():
                person["avatar_path"] = ""
                cleared_avatars.append(person)
                changed = True
            kept.append(person)
        if changed:
            state["people"] = kept
            state["version"] = 4
            self._write(state)
        return {
            "kept": kept,
            "removed": removed,
            "cleared_avatars": cleared_avatars,
        }

    def remove_material_record(self, material_id):
        state = self.load()
        before = len(state["materials"])
        state["materials"] = [
            item for item in state["materials"] if item["id"] != material_id
        ]
        if len(state["materials"]) == before:
            raise KeyError("找不到要移除的素材")
        self._write(state)

    def check_materials(self):
        state = self.load()
        kept = []
        removed = []
        for material in state["materials"]:
            path = Path(material["path"])
            if not path.exists():
                removed.append({**material, "check_reason": "保存位置不存在"})
                continue
            try:
                has_files = material_directory_has_files(path)
            except OSError:
                kept.append(material)
                continue
            if not has_files:
                removed.append({**material, "check_reason": "素材目录已空"})
                continue
            kept.append(material)
        if removed:
            state["materials"] = kept
            self._write(state)
        return {"kept": kept, "removed": removed}


def material_directory_has_files(path):
    path = Path(path)
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    return any(child.is_file() for child in path.rglob("*"))


def material_directory_stats(path):
    path = Path(path)
    if not path.exists():
        return {"status": "保存位置不存在", "file_count": 0, "size_bytes": 0}
    if path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {"status": "正常", "file_count": 1, "size_bytes": size}
    file_count = 0
    size_bytes = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                file_count += 1
                try:
                    size_bytes += child.stat().st_size
                except OSError:
                    pass
    except OSError as error:
        return {
            "status": f"无法检查：{error}",
            "file_count": file_count,
            "size_bytes": size_bytes,
        }
    return {
        "status": "正常" if file_count else "目录已空",
        "file_count": file_count,
        "size_bytes": size_bytes,
    }
