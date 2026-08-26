import shlex
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path


SHANA_PREFIX = "[SHANA]"
MAX_COMPRESSED_FILE_NAME_LENGTH = 180


def read_shana_preset(preset_path):
    tree = ET.parse(preset_path)
    root = tree.getroot()

    prefix = root.findtext("prefixtextBox") or SHANA_PREFIX
    filter_v = root.findtext("filterparamBoxV") or ""
    filter_a = root.findtext("filterparamBoxA") or ""
    enc_param = root.findtext("encparamBox") or ""

    prefix = prefix.strip() or SHANA_PREFIX
    return prefix, filter_v.strip(), filter_a.strip(), enc_param.strip()


def should_compress(file_path, keywords):
    if not keywords:
        return False

    file_name = Path(file_path).name.lower()
    for keyword in keywords:
        if str(keyword).strip().lower() in file_name:
            return True

    return False


def make_compressed_file_name(src_file, prefix):
    src_file = Path(src_file)
    file_name = f"{prefix}{src_file.stem}.mp4"

    if len(file_name) <= MAX_COMPRESSED_FILE_NAME_LENGTH:
        return file_name

    keep_stem_length = MAX_COMPRESSED_FILE_NAME_LENGTH - len(prefix) - len(".mp4")
    keep_stem_length = max(20, keep_stem_length)
    short_name = f"{prefix}{src_file.stem[:keep_stem_length]}.mp4"
    print(f"文件名太长，压缩版文件名截短为：{short_name}")
    return short_name


def get_compressed_path(src_file, preset_path):
    prefix, _, _, _ = read_shana_preset(preset_path)
    src_file = Path(src_file)
    return src_file.parent / make_compressed_file_name(src_file, prefix)


def compressed_is_new(src_file, dst_file):
    if not dst_file.exists():
        return False

    if dst_file.stat().st_size <= 0:
        return False

    return dst_file.stat().st_mtime_ns >= src_file.stat().st_mtime_ns


def clean_shana_args(args):
    cleaned = []
    index = 0

    while index < len(args):
        item = args[index]
        item_lower = item.strip().lower()

        if item_lower.startswith("-tune") and index + 1 < len(args):
            value = args[index + 1].strip().strip('"').strip("'").lower()
            if value == "none":
                print(f"Shana 预设里有 {item} none，命令行无效，已自动跳过。")
                index += 2
                continue

        if item_lower.startswith("-tune") and "=" in item_lower:
            value = item_lower.split("=", 1)[1].strip().strip('"').strip("'")
            if value == "none":
                print(f"Shana 预设里有 {item}，命令行无效，已自动跳过。")
                index += 1
                continue

        if item_lower.startswith("-level") and index + 1 < len(args):
            value = args[index + 1].strip().strip('"').strip("'").lower()
            if value == "auto":
                print(f"Shana 预设里有 {item} auto，命令行无效，已自动跳过。")
                index += 2
                continue

        if item_lower.startswith("-level") and "=" in item_lower:
            value = item_lower.split("=", 1)[1].strip().strip('"').strip("'")
            if value == "auto":
                print(f"Shana 预设里有 {item}，命令行无效，已自动跳过。")
                index += 1
                continue

        cleaned.append(item)
        index += 1

    return cleaned


def build_ffmpeg_args(shana_ffmpeg_path, preset_path, src_file, dst_file):
    _, filter_v, filter_a, enc_param = read_shana_preset(preset_path)

    # shanasubtitle 是 Shana GUI 里的专用滤镜，命令行直接跑可能失败，这里跳过。
    if "shanasubtitle" in filter_v:
        filter_v = ""

    args = [str(shana_ffmpeg_path), "-y", "-i", str(src_file)]

    if filter_v:
        args.extend(shlex.split(filter_v, posix=False))

    if filter_a:
        args.extend(shlex.split(filter_a, posix=False))

    if enc_param:
        args.extend(shlex.split(enc_param, posix=False))

    args = clean_shana_args(args)
    args.append(str(dst_file))
    return args


def compress_video(src_file, config):
    src_file = Path(src_file)

    if src_file.name.startswith(SHANA_PREFIX):
        return src_file

    if not config.get("compress_enabled", True):
        return src_file

    shana_ffmpeg_text = str(config.get("shana_ffmpeg_path") or "").strip()
    preset_text = str(config.get("shana_preset_path") or "").strip()
    if not shana_ffmpeg_text or not preset_text:
        print("未配置视频编码器或预设，上传原文件。")
        return src_file
    shana_ffmpeg_path = Path(shana_ffmpeg_text)
    preset_path = Path(preset_text)

    if not preset_path.exists():
        print(f"没找到 Shana 预设，上传原文件：{preset_path}")
        return src_file

    dst_file = get_compressed_path(src_file, preset_path)

    # 不管关键词是否命中，只要已经有最新压缩版，就优先上传压缩版。
    if compressed_is_new(src_file, dst_file):
        print(f"发现压缩版，上传压缩版：{dst_file.name}")
        return dst_file

    keywords = config.get("compress_name_keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]

    if not should_compress(src_file, keywords):
        if dst_file.exists():
            print(f"压缩版已过期但文件名未命中压缩关键词，上传原文件：{src_file.name}")
        return src_file

    if not shana_ffmpeg_path.exists():
        print(f"没找到 Shana 编码器，上传原文件：{shana_ffmpeg_path}")
        return src_file

    print("\n开始压缩视频：")
    print(f"源文件：{src_file}")
    print(f"输出：{dst_file}")

    args = build_ffmpeg_args(shana_ffmpeg_path, preset_path, src_file, dst_file)
    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    recent_lines = deque(maxlen=20)
    if process.stderr:
        for line in process.stderr:
            line = line.strip()
            if not line:
                continue
            recent_lines.append(line)
            if "time=" in line or "frame=" in line:
                print(line[-120:], end="\r")

    code = process.wait()
    print()

    if code != 0:
        print(f"压缩失败，上传原文件：{src_file.name}")
        if recent_lines:
            print("压缩失败最近日志：")
            for line in recent_lines:
                print(line)
        return src_file

    if not dst_file.exists() or dst_file.stat().st_size <= 0:
        print(f"压缩结果无效，上传原文件：{src_file.name}")
        if recent_lines:
            print("压缩最近日志：")
            for line in recent_lines:
                print(line)
        return src_file

    old_size = src_file.stat().st_size / 1024 / 1024
    new_size = dst_file.stat().st_size / 1024 / 1024
    print(f"压缩完成：{src_file.name}  {old_size:.1f}MB -> {new_size:.1f}MB")

    return dst_file




