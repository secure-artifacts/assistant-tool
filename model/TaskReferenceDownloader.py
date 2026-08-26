import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError

from PyQt5 import QtCore

from app_paths import APP_ROOT
from model.GoogleDriveDownloader import (
    DownloadError,
    download_one,
    parse_drive_link,
)


GOOGLE_DRIVE_URL_PATTERN = re.compile(
    r"https?://(?:drive\.google\.com|drive\.usercontent\.google\.com|docs\.google\.com)"
    r"/[^\s<>\"']+",
    re.IGNORECASE,
)
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEFAULT_EXPORT_FORMATS = {
    "document": "docx",
    "spreadsheets": "xlsx",
    "presentation": "pptx",
    "drawings": "pdf",
}
DEFAULT_REFERENCE_CACHE_FILE = APP_ROOT / "TaskReferenceDownloadState.json"


@dataclass(frozen=True)
class TaskReferenceJob:
    task_id: str
    url: str
    output_dir: Path


class TaskReferenceDownloadCache:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_REFERENCE_CACHE_FILE)
        self.warning = ""
        self.dirty = False
        self.state = self._load()

    @staticmethod
    def _default_state():
        return {"version": 1, "entries": {}}

    def _load(self):
        if not self.path.exists():
            return self._default_state()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            self.warning = "参考文件下载索引无法读取，将重新核对：{}".format(error)
            return self._default_state()
        if not isinstance(state, dict) or not isinstance(state.get("entries"), dict):
            self.warning = "参考文件下载索引格式不正确，将重新核对。"
            return self._default_state()
        return {"version": 1, "entries": state["entries"]}

    @staticmethod
    def job_key(job):
        link = parse_drive_link(job.url)
        identity = "{}:{}".format(link.google_type or "file", link.file_id)
        raw = "{}\0{}".format(str(job.output_dir.resolve()).casefold(), identity)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def cached_path(self, job):
        try:
            key = self.job_key(job)
        except (DownloadError, OSError, ValueError):
            return None
        entry = self.state["entries"].get(key)
        if not isinstance(entry, dict):
            return None
        path_text = str(entry.get("path", "") or "").strip()
        if not path_text:
            self.state["entries"].pop(key, None)
            self.dirty = True
            return None
        try:
            output_dir = job.output_dir.resolve()
            target = Path(path_text).resolve()
            inside_output = os.path.commonpath((str(output_dir), str(target))) == str(output_dir)
        except (OSError, ValueError):
            inside_output = False
            target = None
        if inside_output and target is not None and target.is_file():
            return target
        self.state["entries"].pop(key, None)
        self.dirty = True
        return None

    def remember(self, job, target):
        target = Path(target).resolve()
        output_dir = job.output_dir.resolve()
        if os.path.commonpath((str(output_dir), str(target))) != str(output_dir):
            raise ValueError("参考文件不在对应任务目录内，不能写入下载索引")
        stat = target.stat()
        self.state["entries"][self.job_key(job)] = {
            "url": job.url,
            "path": str(target),
            "size": stat.st_size,
            "completed_at": time.time(),
        }
        self.dirty = True
        self.save()

    def save(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(self.path.name + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temp_path), str(self.path))
            self.dirty = False
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise


def extract_google_drive_urls(value):
    text = str(value or "")
    urls = []
    seen = set()
    for match in GOOGLE_DRIVE_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}，。；：！？）】}")
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def safe_path_component(value, label):
    component = INVALID_PATH_CHARS.sub("_", str(value or "")).strip().rstrip(". ")
    if component in {"", ".", ".."}:
        raise ValueError("{}为空或不能用作目录名".format(label))
    return component


def task_directory_for(root_dir, task_type, task_id):
    root = Path(root_dir).resolve()
    target = root.joinpath(
        safe_path_component(task_type, "任务类型"),
        safe_path_component(task_id, "任务序号"),
    ).resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("任务目录超出了当天任务目录")
    return target


def build_task_reference_jobs(tasks, root_dir):
    jobs = []
    seen = set()
    warnings = []
    for task in tasks:
        urls = extract_google_drive_urls(
            getattr(task, "task_reference_link", "")
        )
        if not urls:
            continue
        try:
            output_dir = task_directory_for(
                root_dir,
                getattr(task, "task_type", ""),
                getattr(task, "task_id", ""),
            )
        except ValueError as error:
            warnings.append(
                "任务 {} 的参考链接已跳过：{}".format(
                    getattr(task, "task_id", "?"), error
                )
            )
            continue

        for url in urls:
            key = (str(output_dir).casefold(), url)
            if key in seen:
                continue
            seen.add(key)
            jobs.append(TaskReferenceJob(str(task.task_id), url, output_dir))
    return jobs, warnings


def partition_cached_reference_jobs(jobs, cache_path=None):
    cache = TaskReferenceDownloadCache(cache_path)
    pending = []
    cached = []
    for job in jobs:
        target = cache.cached_path(job)
        if target is None:
            pending.append(job)
        else:
            cached.append((job, target))
    warnings = [cache.warning] if cache.warning else []
    try:
        cache.save()
    except OSError as error:
        warnings.append("参考文件下载索引无法更新：{}".format(error))
    return pending, cached, warnings


class TaskReferenceDownloadThread(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(object)

    def __init__(self, jobs, parent=None, retries=3, timeout=60, cache_path=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self.retries = max(1, int(retries))
        self.timeout = max(1, int(timeout))
        self.cache_path = cache_path

    def run(self):
        result = {
            "total": len(self.jobs),
            "downloaded": 0,
            "repaired": 0,
            "skipped": 0,
            "cached": 0,
            "failed": 0,
        }
        cache = TaskReferenceDownloadCache(self.cache_path)
        if cache.warning:
            self.log.emit(cache.warning)
        for index, job in enumerate(self.jobs, 1):
            if self.isInterruptionRequested():
                break

            cached_target = cache.cached_path(job)
            if cached_target is not None:
                result["cached"] += 1
                self.log.emit(
                    "任务 {} 参考文件命中本地索引，不访问 Google：{}".format(
                        job.task_id, cached_target.name
                    )
                )
                continue

            self.log.emit(
                "参考文件 [{}/{}] 任务 {}：开始后台下载".format(
                    index, len(self.jobs), job.task_id
                )
            )
            try:
                job.output_dir.mkdir(parents=True, exist_ok=True)
                drive_link = parse_drive_link(job.url)
            except (DownloadError, OSError, ValueError) as error:
                result["failed"] += 1
                self.log.emit("任务 {} 参考文件失败：{}".format(job.task_id, error))
                continue

            for attempt in range(1, self.retries + 1):
                try:
                    status, target = download_one(
                        drive_link,
                        job.output_dir,
                        overwrite=False,
                        timeout=self.timeout,
                        export_formats=DEFAULT_EXPORT_FORMATS,
                    )
                    if status == "skipped":
                        result["skipped"] += 1
                        self.log.emit(
                            "任务 {} 参考文件已存在，跳过：{}".format(
                                job.task_id, target.name
                            )
                        )
                    elif status == "renamed":
                        result["repaired"] += 1
                        self.log.emit(
                            "任务 {} 参考文件乱码名已修正：{}".format(
                                job.task_id, target.name
                            )
                        )
                    else:
                        result["downloaded"] += 1
                        self.log.emit(
                            "任务 {} 参考文件已下载：{}".format(
                                job.task_id, target.name
                            )
                        )
                    try:
                        cache.remember(job, target)
                    except (OSError, ValueError) as error:
                        self.log.emit(
                            "任务 {} 已完成，但下载索引保存失败：{}".format(
                                job.task_id, error
                            )
                        )
                    break
                except (DownloadError, HTTPError, URLError, TimeoutError, OSError) as error:
                    if attempt < self.retries:
                        delay = min(2 ** (attempt - 1), 8)
                        self.log.emit(
                            "任务 {} 下载第 {} 次失败：{}；{} 秒后重试".format(
                                job.task_id, attempt, error, delay
                            )
                        )
                        time.sleep(delay)
                    else:
                        result["failed"] += 1
                        self.log.emit(
                            "任务 {} 参考文件下载失败：{}".format(
                                job.task_id, error
                            )
                        )

        self.completed.emit(result)
