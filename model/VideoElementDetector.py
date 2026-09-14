import base64
import getpass
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
RUNTIME_CONFIG: Dict[str, Any] = {}

DEFAULT_TARGET_NAME = "目标元素"
DEFAULT_REPORT_NAME = "视频检测报告.json"
SAMPLE_INTERVAL_SECONDS = 1.0
SCENE_CHANGE_THRESHOLD = 0.075
FORCE_KEEP_EVERY_SECONDS = 8.0
MAX_FRAMES = 24
FRAME_WIDTH = 320
JPEG_QUALITY = 85


def require_module(module_name: str, install_hint: str):
    try:
        return __import__(module_name)
    except ImportError:
        raise SystemExit(f"缺少依赖：{module_name}\n请先安装：{install_hint}")


def set_runtime_config(config: Optional[Dict[str, Any]]) -> None:
    """Use settings supplied by the main application for the current run."""
    global RUNTIME_CONFIG
    RUNTIME_CONFIG = dict(config or {})


def load_config() -> Dict[str, Any]:
    return dict(RUNTIME_CONFIG)


def config_text(name: str, default: str = "") -> str:
    value = load_config().get(name, default)
    return str(value or default).strip()


def config_text_list(name: str) -> List[str]:
    value = load_config().get(name) or []
    if isinstance(value, str):
        value = re.split(r"[,;；，\n]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def get_target_name() -> str:
    return config_text("video_detection_target_name", DEFAULT_TARGET_NAME)


def get_target_description() -> str:
    return config_text("video_detection_target_description")


def get_report_name() -> str:
    name = Path(config_text("video_detection_report_name", DEFAULT_REPORT_NAME)).name
    return name if name.lower().endswith(".json") else f"{name}.json"


def get_gemini_model() -> str:
    config = load_config()
    return str(config.get("gemini_model") or DEFAULT_GEMINI_MODEL).strip()


def read_gemini_api_keys(api_keys: Optional[List[str]] = None) -> List[str]:
    keys = [key.strip() for key in (api_keys or []) if key.strip()]

    config = load_config()
    config_keys = config.get("gemini_api_keys") or []
    if isinstance(config_keys, str):
        config_keys = [config_keys]
    keys.extend([str(key).strip() for key in config_keys if str(key).strip()])

    env_value = os.environ.get(GEMINI_API_KEY_ENV, "").strip()
    if env_value:
        keys.extend([part.strip() for part in re.split(r"[,;；，\s]+", env_value) if part.strip()])

    seen = set()
    unique_keys = []
    for key in keys:
        if key not in seen:
            unique_keys.append(key)
            seen.add(key)

    if unique_keys:
        return unique_keys

    key = getpass.getpass("请输入 Gemini API Key（不会保存）：").strip()
    if not key:
        raise SystemExit("Gemini API Key 不能为空")

    return [key]


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minute = seconds // 60
    second = seconds % 60
    return f"{minute:02d}:{second:02d}"


def choose_timestamps(duration: float) -> List[float]:
    if duration <= 0:
        return [0.0]

    timestamps = []
    current = 0.0
    while current < duration:
        timestamps.append(current)
        current += SAMPLE_INTERVAL_SECONDS

    return timestamps or [0.0]


def frame_signature(frame) -> Any:
    cv2 = require_module("cv2", "pip install opencv-python")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (3, 3), 0)
    return small


def frame_difference(sig_a, sig_b) -> float:
    cv2 = require_module("cv2", "pip install opencv-python")
    diff = cv2.absdiff(sig_a, sig_b)
    return float(diff.mean() / 255.0)


def should_keep_frame(
    timestamp: float,
    signature,
    last_kept_signature,
    last_kept_timestamp: float,
) -> Tuple[bool, float, str]:
    if last_kept_signature is None:
        return True, 1.0, "首帧"

    diff = frame_difference(signature, last_kept_signature)
    if diff >= SCENE_CHANGE_THRESHOLD:
        return True, diff, f"相对上一张保留帧变化 {diff:.3f} >= {SCENE_CHANGE_THRESHOLD}"

    if timestamp - last_kept_timestamp >= FORCE_KEEP_EVERY_SECONDS:
        return True, diff, f"兜底保留间隔 {FORCE_KEEP_EVERY_SECONDS}s"

    return False, diff, f"相对上一张保留帧变化 {diff:.3f} < {SCENE_CHANGE_THRESHOLD}"


def extract_changed_frames(video_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cv2 = require_module("cv2", "pip install opencv-python")

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 0
    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frame_count / fps if fps > 0 and frame_count > 0 else 0
    timestamps = choose_timestamps(duration)

    kept_frames = []
    skipped_count = 0
    failed_count = 0
    last_kept_signature = None
    last_kept_timestamp = -999999.0
    diff_values = []

    for candidate_index, timestamp in enumerate(timestamps, start=1):
        if len(kept_frames) >= MAX_FRAMES:
            break

        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = capture.read()
        if not ok or frame is None:
            failed_count += 1
            continue

        signature = frame_signature(frame)
        keep, diff, reason = should_keep_frame(timestamp, signature, last_kept_signature, last_kept_timestamp)
        diff_values.append(diff)

        if not keep:
            skipped_count += 1
            continue

        kept_frames.append(
            {
                "index": len(kept_frames) + 1,
                "candidate_index": candidate_index,
                "timestamp_seconds": round(timestamp, 2),
                "timestamp": format_timestamp(timestamp),
                "difference": round(diff, 4),
                "keep_reason": reason,
                "frame": frame,
            }
        )
        last_kept_signature = signature
        last_kept_timestamp = timestamp

    capture.release()

    if not kept_frames:
        raise RuntimeError("没有抽取到任何可分析视频帧")

    stats = {
        "duration_seconds": round(duration, 2),
        "fps": round(fps, 3) if fps else None,
        "frame_count": int(frame_count) if frame_count else None,
        "candidate_frame_count": len(timestamps),
        "kept_frame_count": len(kept_frames),
        "skipped_similar_frame_count": skipped_count,
        "failed_read_frame_count": failed_count,
        "scene_change_threshold": SCENE_CHANGE_THRESHOLD,
        "force_keep_every_seconds": FORCE_KEEP_EVERY_SECONDS,
        "max_difference_seen": round(max(diff_values), 4) if diff_values else None,
        "avg_difference_seen": round(sum(diff_values) / len(diff_values), 4) if diff_values else None,
    }
    return kept_frames, stats


def build_contact_sheet(frames: List[Dict[str, Any]]) -> bytes:
    cv2 = require_module("cv2", "pip install opencv-python")
    import numpy as np

    tiles = []
    for item in frames:
        frame = item["frame"]
        height, width = frame.shape[:2]
        scale = FRAME_WIDTH / max(1, width)
        new_height = max(1, int(height * scale))
        resized = cv2.resize(frame, (FRAME_WIDTH, new_height), interpolation=cv2.INTER_AREA)

        label_height = 42
        label = np.zeros((label_height, FRAME_WIDTH, 3), dtype=np.uint8)
        label_text = f"#{item['index']} {item['timestamp']} d={item['difference']:.3f}"
        cv2.putText(label, label_text, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(np.vstack([label, resized]))

    max_tile_height = max(tile.shape[0] for tile in tiles)
    normalized_tiles = []
    for tile in tiles:
        if tile.shape[0] < max_tile_height:
            pad_height = max_tile_height - tile.shape[0]
            pad = np.zeros((pad_height, tile.shape[1], 3), dtype=np.uint8)
            tile = np.vstack([tile, pad])
        normalized_tiles.append(tile)

    columns = min(4, len(normalized_tiles))
    rows = math.ceil(len(normalized_tiles) / columns)
    sheet_height = rows * max_tile_height
    sheet_width = columns * FRAME_WIDTH
    sheet = np.zeros((sheet_height, sheet_width, 3), dtype=np.uint8)

    for index, tile in enumerate(normalized_tiles):
        row = index // columns
        col = index % columns
        top = row * max_tile_height
        left = col * FRAME_WIDTH
        sheet[top : top + tile.shape[0], left : left + tile.shape[1]] = tile

    ok, encoded = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("抽帧拼图编码失败")

    return encoded.tobytes()


def parse_json_from_text(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def extract_gemini_text(response_json: Dict[str, Any]) -> str:
    candidates = response_json.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini 没有返回 candidates：{response_json}")

    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "") for part in parts if part.get("text")]
    if not texts:
        raise RuntimeError(f"Gemini 没有返回文本：{response_json}")

    return "\n".join(texts)


def call_gemini_once(api_key: str, contact_sheet_jpeg: bytes, prompt: str) -> Dict[str, Any]:
    url = GEMINI_ENDPOINT.format(model=get_gemini_model(), api_key=api_key)
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(contact_sheet_jpeg).decode("ascii")}},
                ],
            }
        ],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


GEMINI_KEY_BLOCK_UNTIL: Dict[str, float] = {}
GEMINI_KEY_BLOCK_REASON: Dict[str, str] = {}


def parse_gemini_retry_seconds(body: str) -> Optional[int]:
    match = re.search(r"retry in\s+([0-9.]+)s", body, flags=re.I)
    if not match:
        return None

    try:
        return max(1, int(math.ceil(float(match.group(1)))))
    except ValueError:
        return None


def gemini_key_is_blocked(api_key: str) -> bool:
    until = GEMINI_KEY_BLOCK_UNTIL.get(api_key)
    if not until:
        return False

    if until <= time.time():
        GEMINI_KEY_BLOCK_UNTIL.pop(api_key, None)
        GEMINI_KEY_BLOCK_REASON.pop(api_key, None)
        return False

    return True


def block_gemini_key(api_key: str, key_index: int, seconds: int, reason: str) -> None:
    seconds = max(1, int(seconds))
    until = time.time() + seconds

    old_until = GEMINI_KEY_BLOCK_UNTIL.get(api_key, 0)
    if old_until < until:
        GEMINI_KEY_BLOCK_UNTIL[api_key] = until
        GEMINI_KEY_BLOCK_REASON[api_key] = reason

    print(f"key #{key_index} 暂停使用 {seconds}s：{reason}")


def gemini_error_text(code: int, body: str) -> str:
    try:
        data = json.loads(body)
        error = data.get("error", {})
        status = error.get("status") or ""
        message = error.get("message") or body
        return f"HTTP {code} {status}: {message[:220]}"
    except Exception:
        return f"HTTP {code}: {body[:220]}"


def result_text_for_check(result: Dict[str, Any]) -> str:
    parts = [str(result.get("summary") or "")]
    for item in result.get("matched_frames") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("reason") or ""))
    return "\n".join(parts)


def tighten_detection_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("found"):
        return result

    positive_keywords = config_text_list("video_detection_positive_keywords")
    negative_keywords = config_text_list("video_detection_negative_keywords")
    ambiguous_keywords = config_text_list("video_detection_ambiguous_keywords")
    if not positive_keywords:
        return result

    text = result_text_for_check(result).casefold()
    has_target_word = any(word.casefold() in text for word in positive_keywords)
    has_non_target_word = any(word.casefold() in text for word in negative_keywords)
    has_ambiguous_word = any(word.casefold() in text for word in ambiguous_keywords)

    if has_target_word:
        return result

    if has_non_target_word or has_ambiguous_word:
        target_name = get_target_name()
        result["found"] = False
        result["confidence"] = min(float(result.get("confidence") or 0), 0.3)
        old_summary = str(result.get("summary") or "")
        result["summary"] = f"未明确识别为{target_name}，按未命中处理。{old_summary}".strip()
        result["matched_frames"] = []
        result["post_checked"] = f"found=true 但理由未明确指出{target_name}，已改为 found=false"

    return result

def ask_gemini(contact_sheet_jpeg: bytes, frames: List[Dict[str, Any]], video_path: Path, stats: Dict[str, Any], api_keys: List[str]) -> Dict[str, Any]:
    target_name = get_target_name()
    target_description = get_target_description()
    if not target_description:
        raise RuntimeError(
            "未配置 video_detection_target_description，无法使用 AI 视频检测"
        )
    configured_rules = config_text_list("video_detection_rules")
    if configured_rules:
        rule_lines = "\n".join(
            f"{index}. {rule}" for index, rule in enumerate(configured_rules, start=1)
        )
    else:
        rule_lines = (
            f"1. 只有画面中明确出现{target_name}时才允许 found=true。\n"
            "2. 无法确认时必须 found=false，confidence 不超过 0.3。\n"
            "3. found=true 时必须在 reason 中说明依据。"
        )
    frame_lines = [
        f"#{item['index']}: {item['timestamp']} ({item['timestamp_seconds']}s), difference={item['difference']}, reason={item['keep_reason']}"
        for item in frames
    ]
    prompt = f"""
你是视频抽帧画面审核助手。请只根据这张抽帧拼图判断视频中是否出现目标元素。

目标元素名称：{target_name}
目标元素说明：{target_description}

判定要求：
{rule_lines}
拼图每格左上角有帧编号、时间戳和画面差异值。请返回最可能命中的帧编号和时间戳。
只返回 JSON，不要 Markdown，不要解释 JSON 之外的文字。

视频文件：{video_path.name}
视频时长约：{stats['duration_seconds']} 秒
候选抽帧数：{stats['candidate_frame_count']}
实际分析帧数：{stats['kept_frame_count']}
跳过相似帧数：{stats['skipped_similar_frame_count']}
抽帧列表：
{chr(10).join(frame_lines)}

JSON 格式：
{{
  "found": true,
  "confidence": 0.0,
  "matched_frames": [{{"frame": 1, "timestamp": "00:00", "reason": "简短原因"}}],
  "summary": "一句话说明"
}}
""".strip()

    errors = []
    for index, api_key in enumerate(api_keys, start=1):
        if gemini_key_is_blocked(api_key):
            remain_seconds = int(GEMINI_KEY_BLOCK_UNTIL[api_key] - time.time())
            reason = GEMINI_KEY_BLOCK_REASON.get(api_key, "之前已经失败")
            errors.append(f"key #{index}: 本轮跳过，{reason}，约 {remain_seconds}s 后再试")
            continue

        for attempt in range(2):
            try:
                response_json = call_gemini_once(api_key, contact_sheet_jpeg, prompt)
                content = extract_gemini_text(response_json)
                result = tighten_detection_result(parse_json_from_text(content))
                result["model"] = get_gemini_model()
                result["target_name"] = target_name
                result["target_description"] = target_description
                result["video_path"] = str(video_path)
                result["frame_selection"] = stats
                result["sampled_frames"] = [
                    {
                        "frame": item["index"],
                        "timestamp": item["timestamp"],
                        "timestamp_seconds": item["timestamp_seconds"],
                        "difference": item["difference"],
                        "keep_reason": item["keep_reason"],
                    }
                    for item in frames
                ]
                return result
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                error_text = gemini_error_text(exc.code, body)
                retry_seconds = parse_gemini_retry_seconds(body)

                if exc.code == 429:
                    if retry_seconds and retry_seconds <= 20 and attempt == 0:
                        print(f"key #{index} 触发短暂限速，等待 {retry_seconds}s 后重试一次。")
                        time.sleep(retry_seconds + 1)
                        continue

                    block_seconds = (retry_seconds + 5) if retry_seconds else 600
                    block_gemini_key(api_key, index, block_seconds, "429 额度或频率限制")
                elif exc.code == 403:
                    block_gemini_key(api_key, index, 24 * 60 * 60, "403 项目无权限")

                errors.append(f"key #{index}: {error_text}")
                break
            except Exception as exc:
                errors.append(f"key #{index}: {type(exc).__name__}: {exc}")
                break

    raise RuntimeError("所有 Gemini API Key 当前都不可用：\n" + "\n".join(errors[-20:]))




COMPRESSED_VIDEO_PREFIX = "[SHANA]"
MANUAL_ACTION_NORMAL_UPLOAD = "normal_upload"
MANUAL_ACTION_REVIEW = "review"
MANUAL_ACTION_SKIP_UPLOAD = "skip_upload"


def normalize_detection_mode(mode: Optional[str] = None) -> str:
    text = str(mode or "").strip().lower()
    if text in {"ai", "gemini", "auto", "自动", "ai检测"}:
        return "ai"
    if text in {"skip", "none", "no", "false", "不检测", "跳过"}:
        return "skip"
    if text in {"manual", "human", "人工", "人工审核"}:
        return "manual"
    return "ai"


def ask_manual_review_result(video_path: Path, contact_sheet_jpeg: bytes) -> str:
    import tkinter as tk
    from tkinter import messagebox

    cv2 = require_module("cv2", "pip install opencv-python")
    import numpy as np

    image_data = np.frombuffer(contact_sheet_jpeg, dtype=np.uint8)
    original_image = cv2.imdecode(image_data, cv2.IMREAD_COLOR)
    if original_image is None:
        raise RuntimeError("无法读取人工审核抽帧图")

    target_name = get_target_name()
    root = tk.Tk()
    root.title(f"人工审核{target_name}")
    root.configure(bg="#f3f4f6")
    root.minsize(900, 650)

    try:
        root.state("zoomed")
    except tk.TclError:
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        root.geometry(f"{int(screen_width * 0.9)}x{int(screen_height * 0.9)}")

    title = tk.Label(
        root,
        text=f"请判断视频中是否出现{target_name}：{Path(video_path).name}",
        bg="#f3f4f6",
        fg="#111827",
        font=("Microsoft YaHei UI", 12, "bold"),
        anchor="w",
        padx=14,
        pady=10,
        wraplength=max(700, root.winfo_screenwidth() - 100),
    )
    title.pack(fill="x")

    image_frame = tk.Frame(root, bg="#1f2937")
    image_frame.pack(fill="both", expand=True, padx=12)

    canvas = tk.Canvas(image_frame, bg="#111827", highlightthickness=0)
    horizontal_scrollbar = tk.Scrollbar(image_frame, orient="horizontal", command=canvas.xview)
    vertical_scrollbar = tk.Scrollbar(image_frame, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal_scrollbar.set, yscrollcommand=vertical_scrollbar.set)

    horizontal_scrollbar.pack(side="bottom", fill="x")
    vertical_scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    original_height, original_width = original_image.shape[:2]
    viewer = {"scale": 1.0, "photo": None, "image_id": None}

    def render_image(new_scale: float) -> None:
        new_scale = max(0.1, min(5.0, new_scale))
        old_width = max(1, int(original_width * viewer["scale"]))
        old_height = max(1, int(original_height * viewer["scale"]))
        center_x = canvas.canvasx(canvas.winfo_width() / 2) / old_width
        center_y = canvas.canvasy(canvas.winfo_height() / 2) / old_height

        width = max(1, int(original_width * new_scale))
        height = max(1, int(original_height * new_scale))
        interpolation = cv2.INTER_AREA if new_scale < 1.0 else cv2.INTER_LINEAR
        resized = cv2.resize(original_image, (width, height), interpolation=interpolation)
        ok, encoded_png = cv2.imencode(".png", resized)
        if not ok:
            raise RuntimeError("人工审核图片缩放失败")

        photo = tk.PhotoImage(data=base64.b64encode(encoded_png.tobytes()).decode("ascii"))
        if viewer["image_id"] is None:
            viewer["image_id"] = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            canvas.itemconfigure(viewer["image_id"], image=photo)

        viewer["photo"] = photo
        viewer["scale"] = new_scale
        canvas.configure(scrollregion=(0, 0, width, height))

        left = max(0.0, min(1.0, (center_x * width - canvas.winfo_width() / 2) / width))
        top = max(0.0, min(1.0, (center_y * height - canvas.winfo_height() / 2) / height))
        canvas.xview_moveto(left)
        canvas.yview_moveto(top)

    def fit_image() -> None:
        root.update_idletasks()
        available_width = max(1, canvas.winfo_width() - 4)
        available_height = max(1, canvas.winfo_height() - 4)
        render_image(min(available_width / original_width, available_height / original_height, 1.0))
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)

    def zoom(factor: float) -> None:
        render_image(viewer["scale"] * factor)

    def on_mouse_wheel(event) -> str:
        zoom(1.15 if event.delta > 0 else 1 / 1.15)
        return "break"

    controls = tk.Frame(root, bg="#f3f4f6", padx=12, pady=10)
    controls.pack(fill="x")

    zoom_group = tk.Frame(controls, bg="#f3f4f6")
    zoom_group.pack(side="left")
    tk.Button(zoom_group, text="−", width=4, font=("Microsoft YaHei UI", 12), command=lambda: zoom(1 / 1.2)).pack(side="left", padx=(0, 5))
    tk.Button(zoom_group, text="适应窗口", font=("Microsoft YaHei UI", 10), command=fit_image).pack(side="left", padx=5)
    tk.Button(zoom_group, text="+", width=4, font=("Microsoft YaHei UI", 12), command=lambda: zoom(1.2)).pack(side="left", padx=5)
    tk.Label(zoom_group, text="鼠标滚轮可缩放", bg="#f3f4f6", fg="#4b5563", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=8)

    result = {"action": None}

    def finish(action: str) -> None:
        result["action"] = action
        root.destroy()

    review_group = tk.Frame(controls, bg="#f3f4f6")
    review_group.pack(side="right")
    tk.Button(
        review_group,
        text=f"没有{target_name}，正常上传",
        command=lambda: finish(MANUAL_ACTION_NORMAL_UPLOAD),
        bg="#15803d",
        fg="white",
        activebackground="#166534",
        activeforeground="white",
        font=("Microsoft YaHei UI", 11, "bold"),
        padx=18,
        pady=8,
    ).pack(side="left", padx=6)
    tk.Button(
        review_group,
        text=f"有{target_name}，进入审核",
        command=lambda: finish(MANUAL_ACTION_REVIEW),
        bg="#b45309",
        fg="white",
        activebackground="#92400e",
        activeforeground="white",
        font=("Microsoft YaHei UI", 11, "bold"),
        padx=18,
        pady=8,
    ).pack(side="left", padx=6)
    tk.Button(
        review_group,
        text="视频有问题，不上传",
        command=lambda: finish(MANUAL_ACTION_SKIP_UPLOAD),
        bg="#b91c1c",
        fg="white",
        activebackground="#991b1b",
        activeforeground="white",
        font=("Microsoft YaHei UI", 11, "bold"),
        padx=18,
        pady=8,
    ).pack(side="left", padx=(6, 0))

    def prevent_accidental_close() -> None:
        messagebox.showinfo(
            "尚未完成审核",
            "请选择“正常上传”“进入审核”或“视频有问题，不上传”。",
            parent=root,
        )

    root.protocol("WM_DELETE_WINDOW", prevent_accidental_close)
    canvas.bind("<MouseWheel>", on_mouse_wheel)
    root.after(50, fit_image)
    root.lift()
    root.focus_force()
    root.mainloop()

    if result["action"] is None:
        raise RuntimeError("人工审核窗口未返回结果")
    return str(result["action"])


def manual_review_video(video_path: Path) -> Dict[str, Any]:
    frames, stats = extract_changed_frames(video_path)
    contact_sheet_jpeg = build_contact_sheet(frames)
    action = ask_manual_review_result(video_path, contact_sheet_jpeg)
    if isinstance(action, bool):
        action = MANUAL_ACTION_REVIEW if action else MANUAL_ACTION_NORMAL_UPLOAD
    action = str(action or "").strip()
    if action not in {
        MANUAL_ACTION_NORMAL_UPLOAD,
        MANUAL_ACTION_REVIEW,
        MANUAL_ACTION_SKIP_UPLOAD,
    }:
        raise RuntimeError(f"无法识别人工审核结果：{action}")
    found = action == MANUAL_ACTION_REVIEW
    skip_upload = action == MANUAL_ACTION_SKIP_UPLOAD
    if skip_upload:
        summary = "人工审核：视频有问题，不上传"
    elif found:
        summary = f"人工审核：发现{get_target_name()}，进入审核"
    else:
        summary = f"人工审核：未发现{get_target_name()}，正常上传"
    return {
        "found": found,
        "confidence": 1.0 if found else 0.0,
        "summary": summary,
        "video_path": str(video_path),
        "manual_review": True,
        "manual_action": action,
        "skip_upload": skip_upload,
        "mode": "manual",
        "frame_selection": stats,
        "sampled_frames": [
            {
                "frame": item["index"],
                "timestamp": item["timestamp"],
                "timestamp_seconds": item["timestamp_seconds"],
                "difference": item["difference"],
                "keep_reason": item["keep_reason"],
            }
            for item in frames
        ],
    }



def is_compressed_video(video_path: Path) -> bool:
    return Path(video_path).name.startswith(COMPRESSED_VIDEO_PREFIX)


def get_video_timestamp(video_path: Path) -> int:
    return Path(video_path).stat().st_mtime_ns


def detection_report_path(video_path: Path) -> Path:
    return Path(video_path).parent / get_report_name()


def load_detection_report(video_path: Path) -> Dict[str, Any]:
    report_path = detection_report_path(video_path)
    if not report_path.exists():
        return {"videoDict": {}}

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {"videoDict": {}}

    if not isinstance(report, dict):
        return {"videoDict": {}}

    video_dict = report.get("videoDict")
    if not isinstance(video_dict, dict):
        report["videoDict"] = {}

    return report


def read_cached_detection(video_path: Path, detection_mode: Optional[str] = None):
    video_path = Path(video_path)
    report = load_detection_report(video_path)
    item = report.get("videoDict", {}).get(video_path.name)
    if not isinstance(item, dict):
        return None

    if item.get("timestamp") != get_video_timestamp(video_path):
        return None

    if detection_mode == "manual" and item.get("mode") != "manual":
        return None

    found = bool(item.get("found"))
    skip_upload = bool(item.get("skip_upload"))
    if skip_upload:
        cache_text = "视频有问题，不上传"
    else:
        cache_text = "发现" if found else "未发现"
    print(f"读取已有视频检测报告：{video_path.name} -> {cache_text}")
    return {
        "found": found,
        "confidence": 1.0 if found else 0.0,
        "summary": (
            "读取已有检测报告：视频有问题，不上传"
            if skip_upload
            else "读取已有检测报告"
        ),
        "video_path": str(video_path),
        "from_cache": True,
        "skip_upload": skip_upload,
        "mode": item.get("mode") or "cache",
    }


def save_detection_report(video_path: Path, result: Dict[str, Any]) -> None:
    video_path = Path(video_path)
    report = load_detection_report(video_path)
    video_dict = report.setdefault("videoDict", {})
    video_dict[video_path.name] = {
        "found": bool(result.get("found")),
        "skip_upload": bool(result.get("skip_upload")),
        "timestamp": get_video_timestamp(video_path),
        "mode": result.get("mode") or "ai",
    }

    report_path = detection_report_path(video_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"保存视频检测报告：{report_path}")


def detect_video_element(video_path: Path, api_keys: Optional[List[str]] = None, detection_mode: Optional[str] = None) -> Dict[str, Any]:
    video_path = Path(video_path)

    if is_compressed_video(video_path):
        return {
            "found": False,
            "confidence": 0.0,
            "summary": "压缩版视频跳过检测",
            "video_path": str(video_path),
            "skipped": True,
        }

    mode = normalize_detection_mode(detection_mode or load_config().get("video_detection_mode") or "ai")

    if mode == "skip":
        return {
            "found": False,
            "confidence": 0.0,
            "summary": "本次选择不使用 AI 检测，按未命中处理",
            "video_path": str(video_path),
            "skipped_by_user": True,
        }

    old_report = read_cached_detection(video_path, mode)
    if old_report is not None:
        return old_report

    if mode == "manual":
        result = manual_review_video(video_path)
        save_detection_report(video_path, result)
        return result

    keys = read_gemini_api_keys(api_keys)
    frames, stats = extract_changed_frames(video_path)
    contact_sheet_jpeg = build_contact_sheet(frames)
    result = ask_gemini(contact_sheet_jpeg, frames, video_path, stats, keys)
    result["mode"] = "ai"
    save_detection_report(video_path, result)
    return result


def is_positive_detection(result: Dict[str, Any], min_confidence: float = 0.4) -> bool:
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return bool(result.get("found")) and confidence >= min_confidence


def summarize_detection(result: Dict[str, Any]) -> str:
    if result.get("skip_upload"):
        return str(result.get("summary") or "视频有问题，不上传")
    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    found_text = "发现" if result.get("found") else "未发现"
    summary = result.get("summary") or ""
    return f"{found_text}，置信度 {confidence:.2f}。{summary}".strip()

