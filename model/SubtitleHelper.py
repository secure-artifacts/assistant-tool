import re

import stable_whisper
from langdetect import detect, LangDetectException

from model.TextHelper import smart_split_sentences


def check_text_language(text, required_language="sk"):
    """
    检测文本语言
    required_language: 要求的语言代码，如 "sk", "en", "zh" 等
    """
    try:
        detected_language = detect(text)

        print(f"检测到的语言: {detected_language}")

        if detected_language != required_language:
            return False
        return True

    except LangDetectException as e:
        return False

def get_text_language(text):
    """
    检测文本语言
    required_language: 要求的语言代码，如 "sk", "en", "zh" 等
    """
    try:
        detected_language = detect(text)
        return detected_language
    except LangDetectException as e:
        return None

def fix_srt_punctuation(srt_path):
    """修复字幕开头的标点符号，移到上一块结尾"""
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测行首是标点的字幕文本行
        stripped = line.lstrip()
        if stripped and stripped[0] in '،,，.。!！?？;；:：':
            # 把标点拼到上一个非空行结尾
            for j in range(len(result) - 1, -1, -1):
                if result[j].strip() and not re.match(r'^\d+$', result[j].strip()) \
                        and '-->' not in result[j]:
                    result[j] = result[j].rstrip() + stripped[0]
                    line = line.lstrip()[1:].lstrip()  # 去掉行首标点
                    break
        result.append(line)
        i += 1

    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(result))


def generate_srt_whisper_only(audio_path, text, output_srt_path, language="sk",
                               chars_per_line=25, max_lines=2,
                               include_line_breaks=False,
                               max_words_per_block=0,
                               block_gap_ms=None,
                               model=None):



    text = smart_split_sentences(text)
    if model is None:
        model = stable_whisper.load_faster_whisper(
            "base", device="cpu", compute_type="int8"
        )
        result = model.align(audio_path, text=text, language=language)
    else:
        # Reuse the already-loaded faster-whisper instance. Loading a second
        # CTranslate2 model can terminate Python with 0xC0000005 on Windows.
        # Calling stable-whisper's align function directly provides the exact
        # same forced-alignment algorithm without replacing model.transcribe.
        from stable_whisper.alignment import align as stable_align

        result = stable_align(
            model, audio_path, text=text, language=language
        )

    # max_words_per_block 为 0 时保持原来的按字符分块逻辑。
    # 启用单词数控制时先合并，再按单词时间戳均匀分块，避免原始段落边界
    # 产生大量只有一两个词的小块。
    max_words_per_block = max(0, int(max_words_per_block or 0))
    if max_words_per_block:
        result.merge_all_segments()
        result.split_by_length(
            max_words=max_words_per_block,
            even_split=True,
        )
    else:
        result.split_by_length(max_chars=chars_per_line * max_lines)

    result.to_srt_vtt(
        output_srt_path,
        segment_level=True,
        word_level=False
    )

    fix_srt_punctuation(output_srt_path)

    # 按选项决定字幕块内部是否换行；默认保持单行
    with open(output_srt_path, 'r', encoding='utf-8') as f:
        srt_text = f.read()

    fixed = fix_srt_line_length(
        srt_text,
        single_line_chars=chars_per_line,
        max_lines=max_lines,
        include_line_breaks=include_line_breaks,
        max_words_per_block=max_words_per_block,
        block_gap_ms=block_gap_ms,
    )

    with open(output_srt_path, 'w', encoding='utf-8') as f:
        f.write(fixed)



def split_srt_block(text: str, single_line_chars: int, max_lines: int,
                    include_line_breaks: bool = True) -> list[str]:
    """
    递归地将超长字幕文本拆分成多段，每段不超过 single_line_chars * max_lines 个字符。
    拆分时不允许断开单词，优先在中间位置寻找空格。
    include_line_breaks 为 True 时，每项内部可能含 \n 换行；否则保持单行。
    """
    max_chars = single_line_chars * max_lines
    text = text.strip()

    # 1. 总长度合法 → 只需处理内部换行
    if len(text) <= max_chars:
        if include_line_breaks:
            return [wrap_lines(text, single_line_chars, max_lines)]
        return [re.sub(r'\s+', ' ', text)]

    # 2. 从中间向两侧寻找最近的空格作为分割点
    mid = len(text) // 2
    left = text.rfind(' ', 0, mid)   # 向左找
    right = text.find(' ', mid)      # 向右找

    if left == -1 and right == -1:
        # 没有空格（极端情况），强制从中间切
        split_pos = mid
    elif left == -1:
        split_pos = right
    elif right == -1:
        split_pos = left
    else:
        # 取离中间更近的那个
        split_pos = left if (mid - left) <= (right - mid) else right

    part_a = text[:split_pos].strip()
    part_b = text[split_pos:].strip()

    # 递归处理每一半
    return split_srt_block(part_a, single_line_chars, max_lines, include_line_breaks) + \
           split_srt_block(part_b, single_line_chars, max_lines, include_line_breaks)


def wrap_lines(text: str, single_line_chars: int, max_lines: int) -> str:
    """
    将一段字幕文本按 single_line_chars 插入换行，使每行不超过该长度。
    （前提：总长度已 <= single_line_chars * max_lines）
    """
    # 先把已有换行展平，再重新分行
    words = text.replace('\n', ' ').split()
    lines = []
    current = ''
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= single_line_chars:
            current += ' ' + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return '\n'.join(lines)


def split_srt_block_by_words(text: str, max_words: int,
                             include_line_breaks: bool = False,
                             single_line_chars: int = 42,
                             max_lines: int = 2) -> list[str]:
    """按最大单词数均匀切分，尽量避免最后只剩一个词。"""
    words = re.sub(r'\s+', ' ', text).strip().split()
    if not words:
        return []

    max_words = max(1, int(max_words))
    block_count = (len(words) + max_words - 1) // max_words
    base_size, extra = divmod(len(words), block_count)

    segments = []
    position = 0
    for block_index in range(block_count):
        block_size = base_size + (1 if block_index < extra else 0)
        segment = ' '.join(words[position:position + block_size])
        position += block_size
        if include_line_breaks:
            segment = wrap_lines(segment, single_line_chars, max_lines)
        segments.append(segment)
    return segments


def apply_srt_block_gap(blocks: list[dict], block_gap_ms=None) -> None:
    """
    原地设置相邻字幕块的精确间隔。
    None 或负数表示保留原间隔；0 表示上一块结束时间等于下一块开始时间。
    """
    if block_gap_ms is None:
        return
    block_gap_ms = int(block_gap_ms)
    if block_gap_ms < 0:
        return

    for index in range(len(blocks) - 1):
        current_start_ms = _tc_to_ms(blocks[index]['start'])
        next_start_ms = _tc_to_ms(blocks[index + 1]['start'])
        end_ms = max(current_start_ms, next_start_ms - block_gap_ms)
        blocks[index]['end'] = _ms_to_tc(end_ms)


def fix_srt_line_length(srt_text: str,
                        single_line_chars: int = 42,
                        max_lines: int = 2,
                        include_line_breaks: bool = True,
                        max_words_per_block: int = 0,
                        block_gap_ms=None) -> str:
    """
    解析 SRT，对超长字幕块进行拆分与换行修复，返回修复后的 SRT 字符串。

    参数
    ----
    srt_text         : 原始 SRT 字符串
    single_line_chars: 单行最大字符数（默认 42）
    max_lines        : 最多行数（默认 2），二者之积为单块最大字符数
    include_line_breaks: 字幕块内部是否按行宽插入换行
    max_words_per_block: 每个字幕块的最大单词数；0 表示继续按字符数分块
    block_gap_ms      : 相邻字幕块间隔毫秒；None/-1 保留，0 表示首尾相接
    """
    # ── 解析 SRT ──────────────────────────────────────────────
    block_pattern = re.compile(
        r'(\d+)\r?\n'                          # 序号
        r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})'      # 开始时间
        r'\s*-->\s*'
        r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})'      # 结束时间
        r'\r?\n'
        r'([\s\S]*?)(?=\r?\n\r?\n|\Z)',        # 字幕内容
        re.MULTILINE
    )

    blocks = []
    for m in block_pattern.finditer(srt_text):
        blocks.append({
            'start': m.group(2),
            'end':   m.group(3),
            'text':  m.group(4).strip(),
        })

    if not blocks:
        return srt_text  # 解析失败，原样返回

    # ── 拆分超长块 ────────────────────────────────────────────
    new_blocks = []
    for blk in blocks:
        # 把字幕文本里的换行当空格处理，统一衡量总长度
        flat_text = re.sub(r'\s+', ' ', blk['text'])
        if max_words_per_block and max_words_per_block > 0:
            segments = split_srt_block_by_words(
                flat_text,
                max_words_per_block,
                include_line_breaks,
                single_line_chars,
                max_lines,
            )
        else:
            segments = split_srt_block(
                flat_text,
                single_line_chars,
                max_lines,
                include_line_breaks
            )

        if len(segments) == 1:
            new_blocks.append({'start': blk['start'],
                               'end':   blk['end'],
                               'text':  segments[0]})
        else:
            # 将时间轴等分给每个子块
            start_ms = _tc_to_ms(blk['start'])
            end_ms   = _tc_to_ms(blk['end'])
            duration = end_ms - start_ms
            seg_dur  = duration / len(segments)

            for i, seg in enumerate(segments):
                s = _ms_to_tc(start_ms + i * seg_dur)
                e = _ms_to_tc(start_ms + (i + 1) * seg_dur)
                new_blocks.append({'start': s, 'end': e, 'text': seg})

    apply_srt_block_gap(new_blocks, block_gap_ms)

    # ── 重新序号并输出 ────────────────────────────────────────
    out_parts = []
    for idx, blk in enumerate(new_blocks, 1):
        out_parts.append(
            f"{idx}\n{blk['start']} --> {blk['end']}\n{blk['text']}"
        )
    return '\n\n'.join(out_parts) + '\n'


# ── 时间码辅助函数 ─────────────────────────────────────────────

def _tc_to_ms(tc: str) -> float:
    """'HH:MM:SS,mmm' 或 'HH:MM:SS.mmm' → 毫秒（float）"""
    tc = tc.replace(',', '.')
    h, m, rest = tc.split(':')
    s, ms = rest.split('.')
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)

def _ms_to_tc(ms: float) -> str:
    """毫秒 → 'HH:MM:SS,mmm'"""
    ms = int(ms)
    h, remainder = divmod(ms, 3_600_000)
    m, remainder = divmod(remainder, 60_000)
    s, millis    = divmod(remainder, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def fix_srt_punctuation(srt_path):
    """修复字幕开头的标点符号，移到上一块结尾"""
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检测行首是标点的字幕文本行
        stripped = line.lstrip()
        if stripped and stripped[0] in '،,，.。!！?？;；:：':
            # 把标点拼到上一个非空行结尾
            for j in range(len(result) - 1, -1, -1):
                if result[j].strip() and not re.match(r'^\d+$', result[j].strip()) \
                        and '-->' not in result[j]:
                    result[j] = result[j].rstrip() + stripped[0]
                    line = line.lstrip()[1:].lstrip()  # 去掉行首标点
                    break
        result.append(line)
        i += 1

    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(result))



def format_srt_time(seconds):
    """SRT时间格式: 00:00:00,000"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def normalize_text(text):
    """
    标准化文本：在标点符号后添加空格（如果没有的话）
    """
    # 在标点符号后添加空格（如果后面不是空格或结尾）
    text = re.sub(r'([.!?,;:])([^\s])', r'\1 \2', text)
    return text
