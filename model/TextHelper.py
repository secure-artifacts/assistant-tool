import re

def smart_split_sentences(text):
    if not text:
        return []

    protected = []

    def make_protector(prefix):
        def protector(match):
            protected.append(match.group(0))
            return f"__{prefix}_{len(protected) - 1}__"
        return protector

    # 保护 URL
    text = re.sub(r'https?://[^\s]+|www\.[^\s]+', make_protector('URL'), text)

    # 保护邮箱
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', make_protector('EMAIL'), text)

    # 保护省略号（要在小数前面保护，避免被误拆）
    text = text.replace('...', '__ELLIPSIS__')

    # 保护小数
    text = re.sub(r'\b\d+\.\d+\b', make_protector('NUM'), text)

    # 保护数字编号（1. 2. 这种）
    text = re.sub(r'\b(\d+)\.\s', make_protector('LISTNUM'), text)

    # 保护文件名（只保护已知扩展名，避免误伤）
    known_exts = r'\b\w+\.(?:txt|pdf|doc|docx|jpg|png|gif|mp4|mp3|csv|json|xml|html|htm|py|js|ts|zip|tar|gz)\b'
    text = re.sub(known_exts, make_protector('FILE'), text, flags=re.IGNORECASE)

    # 保护常见缩写
    abbreviations = ['Mr', 'Mrs', 'Ms', 'Dr', 'Prof', 'Sr', 'Jr', 'Inc', 'Ltd', 'Co', 'Corp', 'etc', 'vs', 'i.e', 'e.g']
    for abbr in abbreviations:
        text = re.sub(rf'\b{abbr}\.', f'{abbr}__DOT__', text, flags=re.IGNORECASE)

    # 分句：句号后跟空白、大写字母或字符串结尾都切（修复了原来只匹配\s的问题）
    sentences = re.split(r'\.(?=\s|[A-ZÁÄČĎÉÍĽŇÓŔŠŤÚÝŽ]|$)', text)

    # 恢复保护内容
    def restore(s):
        s = s.replace('__DOT__', '.')
        s = s.replace('__ELLIPSIS__', '...')
        for i, prot in enumerate(protected):
            for prefix in ('URL', 'EMAIL', 'FILE', 'NUM', 'LISTNUM'):
                s = s.replace(f'__{prefix}_{i}__', prot)
        return s

    result = []
    sentences = [s.strip() for s in sentences]
    sentences = [s for s in sentences if s]

    for idx, sentence in enumerate(sentences):
        sentence = restore(sentence)
        if not sentence:
            continue
        # 最后一句如果原本没有句号就不加
        is_last = (idx == len(sentences) - 1)
        if not is_last and not sentence.endswith('.'):
            sentence += '.'
        result.append(sentence)

    return "\n".join(result)
