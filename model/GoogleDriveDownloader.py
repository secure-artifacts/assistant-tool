#!/usr/bin/env python3
"""Download public/shared Google Drive files listed in config.json.

The script intentionally uses only Python's standard library.  It keeps the
filename returned by Google Drive (except for characters Windows cannot store).
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    unquote_to_bytes,
    urlencode,
    urljoin,
    urlparse,
)
from urllib.request import HTTPCookieProcessor, Request, build_opener


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_SIZE = 1024 * 1024
WINDOWS_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class DownloadError(RuntimeError):
    """A readable error that should be shown for one configured link."""


@dataclass(frozen=True)
class DriveLink:
    original_url: str
    file_id: str
    resource_key: str | None = None
    google_type: str | None = None


class DownloadFormParser(HTMLParser):
    """Extract Google's large-file confirmation form."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}
        self._inside_selected_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and self.action is None:
            action = values.get("action")
            form_id = values.get("id", "")
            if action and ("download" in action or form_id == "download-form"):
                self.action = html.unescape(action)
                self._inside_selected_form = True
        elif tag == "input" and self._inside_selected_form:
            name = values.get("name")
            if name:
                self.inputs[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._inside_selected_form:
            self._inside_selected_form = False


def parse_drive_link(url: str) -> DriveLink:
    url = url.strip()
    if not url:
        raise DownloadError("链接为空")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise DownloadError("不是有效的 http/https 链接")

    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {
        "drive.google.com",
        "drive.usercontent.google.com",
        "docs.google.com",
    }:
        raise DownloadError("不是受支持的 Google Drive/Docs 链接")

    if "/folders/" in parsed.path:
        raise DownloadError("这是文件夹链接；请把文件夹内的各个文件分享链接分别加入配置")

    query = parse_qs(parsed.query)
    file_id = (query.get("id") or [None])[0]
    resource_key = (query.get("resourcekey") or [None])[0]
    google_type: str | None = None

    path_match = re.search(r"/(?:file/d|d)/([A-Za-z0-9_-]+)", parsed.path)
    if path_match:
        file_id = path_match.group(1)

    if host == "docs.google.com":
        type_match = re.match(
            r"/(document|spreadsheets|presentation|drawings)/d/([A-Za-z0-9_-]+)",
            parsed.path,
        )
        if type_match:
            google_type, file_id = type_match.groups()

    if not file_id or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        raise DownloadError("无法从链接中识别文件 ID")
    return DriveLink(url, file_id, resource_key, google_type)


def export_url(link: DriveLink, export_formats: dict[str, str]) -> str:
    if link.google_type:
        fmt = export_formats[link.google_type]
        if link.google_type == "drawings":
            return f"https://docs.google.com/drawings/d/{link.file_id}/export?format={quote(fmt)}"
        return (
            f"https://docs.google.com/{link.google_type}/d/{link.file_id}"
            f"/export?format={quote(fmt)}"
        )

    params = {"id": link.file_id, "export": "download", "confirm": "t"}
    if link.resource_key:
        params["resourcekey"] = link.resource_key
    return "https://drive.usercontent.google.com/download?" + urlencode(params)


def _decode_filename_value(value: str) -> str:
    parts = decode_header(value)
    decoded: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _mojibake_score(value: str) -> int:
    score = sum(4 for char in value if "\x80" <= char <= "\x9f")
    for marker in (
        "Ã",
        "Â",
        "â€",
        "ðŸ",
        "ï»¿",
        "å¹",
        "æœ",
        "æ",
        "ä¸",
        "å",
    ):
        score += value.count(marker) * 2
    return score


def repair_mojibake_filename(value: str) -> str:
    """Repair UTF-8 bytes that an HTTP header exposed as Latin-1/CP1252."""
    best = value
    best_score = _mojibake_score(value)
    if best_score == 0:
        return value

    for source_encoding in ("latin-1", "cp1252"):
        try:
            candidate = value.encode(source_encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_score = _mojibake_score(candidate)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score
    return best


def filename_from_headers(headers: object) -> str | None:
    disposition = headers.get("Content-Disposition", "")  # type: ignore[attr-defined]
    match = re.search(r"filename\*=\s*([^']*)''([^;]+)", disposition, re.I)
    if match:
        charset = match.group(1) or "utf-8"
        try:
            return unquote_to_bytes(match.group(2)).decode(charset)
        except (UnicodeDecodeError, LookupError):
            return repair_mojibake_filename(unquote(match.group(2)))
    match = re.search(r'filename\s*=\s*"([^"]+)"', disposition, re.I)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", disposition, re.I)
    if match:
        decoded = _decode_filename_value(match.group(1).strip().strip('"'))
        return repair_mojibake_filename(decoded)
    return None


def safe_filename(name: str, fallback: str) -> tuple[str, bool]:
    """Return a filesystem-safe basename and whether it had to be changed."""
    name = name.replace("\u0000", "").strip()
    name = Path(name).name
    changed = False
    if os.name == "nt":
        cleaned = WINDOWS_INVALID_CHARS.sub("_", name).rstrip(" .")
        changed = cleaned != name
        name = cleaned
        if Path(name).stem.upper() in WINDOWS_RESERVED_NAMES:
            name = "_" + name
            changed = True
    if name in {"", ".", ".."}:
        return fallback, True
    return name, changed


def migrate_legacy_mojibake_file(output_dir: Path, correct_filename: str) -> Path | None:
    """Rename a file saved by the old header decoder without overwriting data."""
    target = output_dir / correct_filename
    if target.exists():
        return None
    for legacy_encoding in ("latin-1", "cp1252"):
        try:
            legacy_name = correct_filename.encode("utf-8").decode(legacy_encoding)
        except UnicodeDecodeError:
            continue
        legacy_target = output_dir / legacy_name
        if legacy_target.is_file():
            legacy_target.replace(target)
            return target
    return None


def human_size(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{value} B"


def print_progress(written: int, total: int | None) -> None:
    if total:
        percent = min(100.0, written * 100 / total)
        message = f"\r    {percent:6.2f}%  {human_size(written)} / {human_size(total)}"
    else:
        message = f"\r    已下载 {human_size(written)}"
    print(message, end="", flush=True)


def copy_response(response: BinaryIO, target: Path, total: int | None) -> None:
    written = 0
    last_update = 0.0
    with target.open("wb") as output:
        while True:
            block = response.read(CHUNK_SIZE)
            if not block:
                break
            output.write(block)
            written += len(block)
            now = time.monotonic()
            if now - last_update >= 0.15:
                print_progress(written, total)
                last_update = now
    print_progress(written, total)
    print()
    if total is not None and written != total:
        raise DownloadError(f"文件大小不完整：预期 {total} 字节，实际 {written} 字节")


def error_from_html(page: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", page, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    lower = text.lower()
    if "too many users have viewed or downloaded" in lower or "download quota" in lower:
        return "Google Drive 下载配额已用完，请稍后再试"
    if "you need access" in lower or "request access" in lower:
        return "没有访问权限；请把文件共享方式设为“知道链接的任何人”"
    if "file does not exist" in lower:
        return "文件不存在，或链接已经失效"
    return (text[:180] + "…") if len(text) > 180 else (text or "Google 未返回文件内容")


def open_download_response(opener: object, url: str, timeout: int) -> BinaryIO:
    """Open a download, following a confirmation form up to three times."""
    current_url = url
    for _ in range(3):
        request = Request(current_url, headers={"User-Agent": USER_AGENT})
        response = opener.open(request, timeout=timeout)  # type: ignore[attr-defined]
        if filename_from_headers(response.headers):
            return response

        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return response

        raw = response.read(2 * 1024 * 1024)
        charset = response.headers.get_content_charset() or "utf-8"
        page = raw.decode(charset, errors="replace")
        response.close()
        parser = DownloadFormParser()
        parser.feed(page)
        if not parser.action:
            raise DownloadError(error_from_html(page))
        action = urljoin(current_url, parser.action)
        separator = "&" if "?" in action else "?"
        current_url = action + (separator + urlencode(parser.inputs) if parser.inputs else "")
    raise DownloadError("Google 的下载确认页面循环次数过多")


def download_one(
    link: DriveLink,
    output_dir: Path,
    overwrite: bool,
    timeout: int,
    export_formats: dict[str, str],
) -> tuple[str, Path]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    response = open_download_response(opener, export_url(link, export_formats), timeout)
    try:
        google_name = filename_from_headers(response.headers) or link.file_id
        filename, changed = safe_filename(google_name, link.file_id)
        target = output_dir / filename
        if changed:
            print(f"    文件名含系统不允许的字符，保存为：{filename}")
        if target.exists() and not overwrite:
            return "skipped", target
        if not overwrite:
            migrated = migrate_legacy_mojibake_file(output_dir, filename)
            if migrated is not None:
                return "renamed", migrated

        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        part = target.with_name(target.name + ".part")
        try:
            copy_response(response, part, total)
            part.replace(target)
        except BaseException:
            if part.exists():
                part.unlink()
            raise
        return "downloaded", target
    finally:
        response.close()


def load_config(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            config = json.load(file)
    except FileNotFoundError as exc:
        raise DownloadError(f"找不到配置文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise DownloadError(f"配置文件 JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc
    if not isinstance(config, dict):
        raise DownloadError("配置文件最外层必须是 JSON 对象")
    return config


def positive_int(config: dict[str, object], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DownloadError(f'配置项 "{key}" 必须是大于 0 的整数')
    return value


def run(config_path: Path) -> int:
    config = load_config(config_path)
    links = config.get("links")
    if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
        raise DownloadError('配置项 "links" 必须是字符串数组')
    links = [item.strip() for item in links if item.strip()]
    if not links:
        raise DownloadError('配置项 "links" 里还没有文件链接')

    output_setting = config.get("download_dir", "downloads")
    if not isinstance(output_setting, str) or not output_setting.strip():
        raise DownloadError('配置项 "download_dir" 必须是目录字符串')
    output_dir = Path(output_setting).expanduser()
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    overwrite = config.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise DownloadError('配置项 "overwrite" 必须是 true 或 false')
    retries = positive_int(config, "retries", 3)
    timeout = positive_int(config, "timeout_seconds", 60)

    default_formats = {
        "document": "docx",
        "spreadsheets": "xlsx",
        "presentation": "pptx",
        "drawings": "pdf",
    }
    configured_formats = config.get("google_formats", {})
    if not isinstance(configured_formats, dict):
        raise DownloadError('配置项 "google_formats" 必须是 JSON 对象')
    export_formats = default_formats | {
        str(key): str(value) for key, value in configured_formats.items()
    }

    print(f"下载目录：{output_dir.resolve()}")
    print(f"待处理：{len(links)} 个链接\n")
    downloaded = skipped = failed = 0
    for index, url in enumerate(links, 1):
        print(f"[{index}/{len(links)}] {url}")
        try:
            drive_link = parse_drive_link(url)
        except DownloadError as exc:
            print(f"    失败：{exc}\n")
            failed += 1
            continue

        for attempt in range(1, retries + 1):
            try:
                status, target = download_one(
                    drive_link, output_dir, overwrite, timeout, export_formats
                )
                if status == "skipped":
                    print(f"    已存在，跳过：{target.name}\n")
                    skipped += 1
                else:
                    print(f"    完成：{target.name}\n")
                    downloaded += 1
                break
            except (DownloadError, HTTPError, URLError, TimeoutError, OSError) as exc:
                if attempt < retries:
                    delay = min(2 ** (attempt - 1), 8)
                    print(f"    第 {attempt} 次失败：{exc}；{delay} 秒后重试…")
                    time.sleep(delay)
                else:
                    print(f"    失败：{exc}\n")
                    failed += 1

    print(f"处理完毕：下载 {downloaded}，跳过 {skipped}，失败 {failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量下载 Google Drive 分享文件")
    parser.add_argument(
        "config",
        nargs="?",
        default=str(Path(__file__).with_name("config.json")),
        help="配置文件路径（默认：脚本旁边的 config.json）",
    )
    args = parser.parse_args()
    try:
        return run(Path(args.config).resolve())
    except DownloadError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已由用户取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
