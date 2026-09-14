"""Download Google Drive files or folders into a material staging directory."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from model.GoogleDriveDownloader import (
    DownloadError,
    download_one,
    parse_drive_link,
    safe_filename,
)
from model.GoogleDriveHelper import load_drive_service


GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
DEFAULT_EXPORT_FORMATS = {
    "document": "docx",
    "spreadsheets": "xlsx",
    "presentation": "pptx",
    "drawings": "pdf",
}
NATIVE_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


@dataclass(frozen=True)
class MaterialDriveLink:
    original_url: str
    file_id: str
    is_folder: bool
    resource_key: str | None = None
    google_type: str | None = None

    @property
    def identity(self):
        kind = "folder" if self.is_folder else (self.google_type or "file")
        return f"{kind}:{self.file_id}"


@dataclass(frozen=True)
class MaterialDownloadResult:
    root_paths: tuple[Path, ...]
    downloaded_files: int
    skipped_files: int
    used_authenticated_api: bool


def parse_material_drive_link(url):
    text = str(url or "").strip()
    parsed = urlparse(text)
    host = parsed.netloc.lower().split(":", 1)[0]
    if parsed.scheme not in {"http", "https"} or host not in {
        "drive.google.com",
        "drive.usercontent.google.com",
        "docs.google.com",
    }:
        raise DownloadError("不是受支持的 Google Drive/Docs 链接")

    folder_match = None
    if host == "drive.google.com":
        folder_match = re.search(r"/folders/([A-Za-z0-9_-]+)", parsed.path)
    if folder_match:
        query = parse_qs(parsed.query)
        resource_key = (
            (query.get("resourcekey") or query.get("resourceKey") or [None])[0]
        )
        return MaterialDriveLink(
            original_url=text,
            file_id=folder_match.group(1),
            is_folder=True,
            resource_key=resource_key,
        )

    parsed_file = parse_drive_link(text)
    return MaterialDriveLink(
        original_url=text,
        file_id=parsed_file.file_id,
        is_folder=False,
        resource_key=parsed_file.resource_key,
        google_type=parsed_file.google_type,
    )


def _report(callback, message):
    if callback is not None:
        callback(str(message))


def _unique_target(parent, name):
    safe_name, _changed = safe_filename(str(name or "素材"), "素材")
    safe_name = safe_name[:180]
    target = Path(parent) / safe_name
    if not target.exists():
        return target
    source = Path(safe_name)
    stem = source.stem or "素材"
    suffix = source.suffix
    index = 2
    while True:
        target = Path(parent) / f"{stem} ({index}){suffix}"
        if not target.exists():
            return target
        index += 1


def _load_authenticated_service(service_factory):
    try:
        return service_factory()
    except SystemExit as error:
        raise DownloadError(str(error)) from error


def _metadata(service, file_id):
    return service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,shortcutDetails(targetId,targetMimeType)",
        supportsAllDrives=True,
    ).execute()


def _folder_children(service, folder_id):
    children = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields=(
                "nextPageToken,files("
                "id,name,mimeType,size,shortcutDetails(targetId,targetMimeType))"
            ),
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        children.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return sorted(
        children,
        key=lambda item: (
            item.get("mimeType") != GOOGLE_FOLDER_MIME,
            str(item.get("name", "")).casefold(),
        ),
    )


def resolve_google_drive_folder_name(url, service_factory=load_drive_service):
    """Return the real Drive folder name for a folder URL."""
    link = parse_material_drive_link(url)
    if not link.is_folder:
        raise DownloadError(
            "勾选自动命名时，素材来源必须包含 Google Drive 文件夹链接"
        )
    try:
        service = _load_authenticated_service(service_factory)
        metadata = _metadata(service, link.file_id)
    except Exception as error:
        if isinstance(error, DownloadError):
            raise
        raise DownloadError(
            f"读取 Google Drive 文件夹名称失败：{error}"
        ) from error
    if str(metadata.get("mimeType") or "") != GOOGLE_FOLDER_MIME:
        raise DownloadError("链接指向的内容不是 Google Drive 文件夹")
    name = str(metadata.get("name") or "").strip()
    if not name:
        raise DownloadError("Google Drive 文件夹没有可用的名称")
    return name


def _download_api_file(service, metadata, output_dir, progress_callback):
    from googleapiclient.http import MediaIoBaseDownload

    mime_type = str(metadata.get("mimeType") or "")
    name = str(metadata.get("name") or metadata.get("id") or "素材")
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        export = NATIVE_EXPORTS.get(mime_type)
        if export is None:
            _report(progress_callback, f"跳过暂不支持导出的 Google 原生项目：{name}")
            return None
        export_mime, extension = export
        if not name.casefold().endswith(extension):
            name += extension
        request = service.files().export_media(
            fileId=metadata["id"],
            mimeType=export_mime,
        )
    else:
        request = service.files().get_media(
            fileId=metadata["id"],
            supportsAllDrives=True,
        )

    target = _unique_target(output_dir, name)
    part = target.with_name(target.name + ".part")
    _report(progress_callback, f"正在下载：{target.name}")
    try:
        with part.open("wb") as stream:
            downloader = MediaIoBaseDownload(stream, request, chunksize=1024 * 1024)
            done = False
            last_percent = -1
            while not done:
                status, done = downloader.next_chunk()
                if status is not None:
                    percent = int(status.progress() * 100)
                    if percent >= last_percent + 10 or percent >= 100:
                        _report(progress_callback, f"正在下载 {target.name}：{percent}%")
                        last_percent = percent
        os.replace(str(part), str(target))
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    _report(progress_callback, f"下载完成：{target.name}")
    return target


def _download_api_item(
    service,
    metadata,
    output_dir,
    progress_callback,
    active_folder_ids,
    preferred_name=None,
):
    mime_type = str(metadata.get("mimeType") or "")
    if mime_type == GOOGLE_SHORTCUT_MIME:
        target_id = str(metadata.get("shortcutDetails", {}).get("targetId") or "")
        if not target_id:
            _report(progress_callback, f"跳过失效的快捷方式：{metadata.get('name', '')}")
            return [], 1
        target_metadata = _metadata(service, target_id)
        return _download_api_item(
            service,
            target_metadata,
            output_dir,
            progress_callback,
            active_folder_ids,
            preferred_name=preferred_name or metadata.get("name"),
        )

    if mime_type != GOOGLE_FOLDER_MIME:
        if preferred_name:
            metadata = dict(metadata)
            metadata["name"] = preferred_name
        target = _download_api_file(
            service,
            metadata,
            output_dir,
            progress_callback,
        )
        return ([target] if target else []), (0 if target else 1)

    folder_id = str(metadata.get("id") or "")
    if folder_id in active_folder_ids:
        _report(progress_callback, f"跳过循环文件夹：{metadata.get('name', folder_id)}")
        return [], 1
    folder_name = preferred_name or metadata.get("name") or folder_id
    target_dir = _unique_target(output_dir, folder_name)
    target_dir.mkdir(parents=True)
    _report(progress_callback, f"正在读取网盘文件夹：{folder_name}")
    downloaded = []
    skipped = 0
    active_folder_ids.add(folder_id)
    try:
        for child in _folder_children(service, folder_id):
            child_files, child_skipped = _download_api_item(
                service,
                child,
                target_dir,
                progress_callback,
                active_folder_ids,
            )
            downloaded.extend(child_files)
            skipped += child_skipped
    finally:
        active_folder_ids.discard(folder_id)
    return downloaded, skipped


def download_google_drive_source(
    url,
    output_dir,
    progress_callback: Callable[[str], None] | None = None,
    service_factory=load_drive_service,
):
    """Download one Drive file/folder and return a structured result.

    Public files use the dependency-free downloader first.  Folders and files
    that need permission fall back to the app's existing Google OAuth token.
    """
    link = parse_material_drive_link(url)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    public_error = None

    if not link.is_folder:
        _report(progress_callback, f"正在尝试直接下载网盘文件：{link.file_id}")
        try:
            parsed_file = parse_drive_link(link.original_url)
            _status, target = download_one(
                parsed_file,
                output_dir,
                overwrite=False,
                timeout=60,
                export_formats=DEFAULT_EXPORT_FORMATS,
            )
            _report(progress_callback, f"网盘文件下载完成：{target.name}")
            return MaterialDownloadResult((target,), 1, 0, False)
        except (DownloadError, HTTPError, URLError, TimeoutError, OSError) as error:
            public_error = error
            _report(progress_callback, "直接下载失败，尝试使用已有 Google 授权…")

    try:
        service = _load_authenticated_service(service_factory)
        metadata = _metadata(service, link.file_id)
        downloaded, skipped = _download_api_item(
            service,
            metadata,
            output_dir,
            progress_callback,
            set(),
        )
    except Exception as error:
        detail = f"；直接下载错误：{public_error}" if public_error else ""
        raise DownloadError(f"Google Drive 下载失败：{error}{detail}") from error

    downloaded = [path for path in downloaded if path is not None]
    if not downloaded:
        raise DownloadError("网盘来源中没有可下载的文件，或文件夹为空")
    roots = tuple(path for path in output_dir.iterdir() if path.name != ".part")
    _report(
        progress_callback,
        f"网盘来源处理完成：下载 {len(downloaded)} 个文件"
        + (f"，跳过 {skipped} 个不支持项目" if skipped else ""),
    )
    return MaterialDownloadResult(roots, len(downloaded), skipped, True)
