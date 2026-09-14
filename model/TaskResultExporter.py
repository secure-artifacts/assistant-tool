import shutil
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

try:
    from model.OdsHelper import ReadTaskOds
except ImportError as exc:
    raise SystemExit(
        "整理模块依赖缺失，通常需要安装 odfpy：\n"
        "pip install odfpy\n"
        f"当前缺少：{getattr(exc, 'name', exc)}"
    )


class ExportResult(NamedTuple):
    output_dir: Path
    updated_files: List[Path]


class _TemplateValues(dict):
    def __missing__(self, key):
        return ""


DEFAULT_OUTPUT_FILENAME_MAX_LENGTH = 50
MIN_OUTPUT_FILENAME_MAX_LENGTH = 12


def folder_name(task_date: date) -> str:
    return f"{task_date.month:02d}{task_date.day:02d}"


def render_template(template: str, task, task_date: date, index=None) -> str:
    values = _TemplateValues(
        year=task_date.year,
        month=task_date.month,
        day=task_date.day,
        mm=f"{task_date.month:02d}",
        dd=f"{task_date.day:02d}",
        index="" if index is None else index,
        task_id=task.task_id,
        admin=task.admin,
        creator=task.creator,
        task_date=task.task_date,
        task_name=task.task_name,
        task_type=task.task_type,
    )
    return str(template or "").format_map(values)


def copy_if_updated(src: Path, dst: Path) -> bool:
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return False

    need_copy = True
    if dst.exists():
        src_stat = src.stat()
        dst_stat = dst.stat()
        need_copy = (
            src_stat.st_mtime_ns > dst_stat.st_mtime_ns
            or src_stat.st_size != dst_stat.st_size
        )
    if not need_copy:
        print(f"跳过未更新: {dst}")
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"已复制: {dst}")
    return True


def copytree_if_updated(
    src_dir: Path,
    dst_dir: Path,
    destination_callback: Optional[Callable[[Path], None]] = None,
) -> List[Path]:
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if not src_dir.exists():
        return []

    updated_files = []
    for src_file in src_dir.rglob("*"):
        if src_file.is_file():
            dst_file = dst_dir / src_file.relative_to(src_dir)
            if copy_if_updated(src_file, dst_file):
                updated_files.append(dst_file)
            if destination_callback is not None:
                destination_callback(dst_file)
    return updated_files


def first_existing_file(task_dir: Path, names: List[str]) -> Optional[Path]:
    for name in names:
        file_path = task_dir / name
        if file_path.exists():
            return file_path
    return None


def output_filename_max_length(config: Optional[Dict] = None) -> int:
    """Return 0 for unlimited, otherwise a safe total filename length."""
    try:
        value = int(
            (config or {}).get(
                "task_output_filename_max_length",
                DEFAULT_OUTPUT_FILENAME_MAX_LENGTH,
            )
        )
    except (TypeError, ValueError):
        value = DEFAULT_OUTPUT_FILENAME_MAX_LENGTH
    if value <= 0:
        return 0
    return max(MIN_OUTPUT_FILENAME_MAX_LENGTH, min(255, value))


def limit_output_filename(file_name: str, max_length: int) -> str:
    """Shorten only the last path component and preserve its extension."""
    value = str(file_name or "")
    if not value or max_length <= 0:
        return value
    path = Path(value)
    name = path.name
    if len(name) <= max_length:
        return value
    suffix = path.suffix
    stem_length = max(1, max_length - len(suffix))
    limited_name = f"{path.stem[:stem_length]}{suffix}"
    parent = path.parent
    return limited_name if str(parent) in {"", "."} else str(parent / limited_name)


def legacy_output_name(
    task, task_date: date, config: Dict, profile=None, index=None
) -> str:
    profile = profile if isinstance(profile, dict) else {}
    template = (
        profile.get("output_name_template")
        or config.get("task_output_name_template")
        or "{task_id}-{task_name}.mp4"
    )
    return render_template(template, task, task_date, index=index)


def output_name(task, task_date: date, config: Dict, profile=None, index=None) -> str:
    original = legacy_output_name(task, task_date, config, profile, index)
    return limit_output_filename(original, output_filename_max_length(config))


def compatible_output_path(directory: Path, current_name: str, legacy_name: str) -> Path:
    """Keep using an existing pre-limit file so old work is not re-exported."""
    directory = Path(directory)
    current_path = directory / current_name
    legacy_path = directory / legacy_name
    if (
        current_path != legacy_path
        and not current_path.exists()
        and legacy_path.exists()
    ):
        return legacy_path
    return current_path


def export_task(
    task,
    task_date: date,
    root_dir: Path,
    output_dir: Path,
    output_dir_wsp: Path,
    profile: Dict,
    config: Dict,
    wsp_export: bool,
    exported_file_callback: Optional[Callable[[Path, Any], None]] = None,
) -> List[Path]:
    def remember_output(file_path: Path) -> None:
        if exported_file_callback is not None:
            exported_file_callback(Path(file_path), task)

    mode = str(profile.get("mode") or "single").strip().lower()
    task_dir_name = render_template(profile.get("task_dir", ""), task, task_date)
    if not task_dir_name:
        return []
    task_dir = root_dir / task_dir_name / task.task_id

    if mode == "single":
        candidate_names = [
            render_template(name, task, task_date)
            for name in profile.get("candidate_names", [])
            if str(name).strip()
        ]
        result_file = first_existing_file(task_dir, candidate_names)
        updated = []
        if result_file:
            legacy_name = legacy_output_name(task, task_date, config, profile)
            current_name = output_name(task, task_date, config, profile)
            dst = compatible_output_path(output_dir, current_name, legacy_name)
            if copy_if_updated(result_file, dst):
                updated.append(dst)
            remember_output(dst)

        wsp_name = str(profile.get("wsp_candidate") or "").strip()
        if wsp_export and wsp_name:
            wsp_file = task_dir / render_template(wsp_name, task, task_date)
            if wsp_file.exists():
                legacy_name = legacy_output_name(task, task_date, config, profile)
                current_name = output_name(task, task_date, config, profile)
                dst = compatible_output_path(
                    output_dir_wsp, current_name, legacy_name
                )
                if copy_if_updated(wsp_file, dst):
                    updated.append(dst)
                remember_output(dst)
        return updated

    if mode == "all_videos":
        if not task_dir.exists():
            return []
        updated = []
        for index, video_file in enumerate(task_dir.rglob("*.mp4"), start=1):
            legacy_name = legacy_output_name(
                task,
                task_date,
                config,
                profile,
                index=index,
            )
            current_name = output_name(
                task, task_date, config, profile, index=index
            )
            dst = compatible_output_path(output_dir, current_name, legacy_name)
            if copy_if_updated(video_file, dst):
                updated.append(dst)
            remember_output(dst)
        return updated

    if mode == "copy_tree":
        if not task_dir.exists():
            return []
        directory_template = (
            profile.get("output_directory_template")
            or config.get("task_output_directory_template")
            or "{task_id}-{task_name}"
        )
        directory_name = render_template(directory_template, task, task_date)
        return copytree_if_updated(
            task_dir,
            output_dir / directory_name,
            destination_callback=remember_output,
        )

    print(f"未知导出模式，已跳过：{mode}")
    return []


def export_one_date(
    task_date: date,
    base_dir=None,
    wsp_export: bool = False,
    config: Optional[Dict] = None,
    exported_file_callback: Optional[Callable[[Path, Any], None]] = None,
) -> Optional[ExportResult]:
    config = dict(config or {})
    if base_dir is None:
        base_dir = config.get("task_path")
    if not base_dir:
        raise ValueError("未配置 task_path")

    root_dir = Path(base_dir) / folder_name(task_date)
    output_dir = root_dir / str(config.get("task_result_output_dir") or "result")
    output_dir_wsp = root_dir / str(
        config.get("task_result_wsp_output_dir") or "result-wsp"
    )
    table_file_name = str(config.get("task_table_file_name") or "tasks.ods")
    doc_path = root_dir / table_file_name
    profiles = config.get("task_export_profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("task_export_profiles 必须是对象")

    print(f"\n开始整理：{task_date:%Y-%m-%d}")
    print(f"任务目录：{root_dir}")
    if not doc_path.exists():
        print(f"没找到登记表，跳过：{doc_path}")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir_wsp.mkdir(parents=True, exist_ok=True)
    updated_files = []
    for task in ReadTaskOds(doc_path):
        if (
            getattr(task, "review_required_text", "")
            and getattr(task, "review_required", None) is None
        ):
            print(
                "无法识别是否需要审核，按未设置处理："
                f"第 {getattr(task, 'source_row', '?')} 行 -> "
                f"{task.review_required_text}"
            )
        profile = profiles.get(task.task_type)
        if not isinstance(profile, dict):
            print(f"未配置该任务类型的导出规则，已跳过：{task.task_type}")
            continue
        task_updated_files = export_task(
            task,
            task_date,
            root_dir,
            output_dir,
            output_dir_wsp,
            profile,
            config,
            wsp_export,
            exported_file_callback=exported_file_callback,
        )
        updated_files.extend(task_updated_files)

    print(f"整理完成：{output_dir}")
    print(f"本次新增/更新文件数：{len(updated_files)}")
    return ExportResult(output_dir=output_dir, updated_files=updated_files)
