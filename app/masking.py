"""Unicode 安全的遮盖工具。

前端浏览器选区偏移以 UTF-16 码元（code unit）计，
因此服务端统一把正文编码为 UTF-16 码元序列处理，
保证表情符号（如 🌍，占两个码元的代理对）等字符的偏移精确一致。
"""

MASK_CHAR = 0x2588  # '█'


def u16_units(text: str) -> list[int]:
    """把字符串转为 UTF-16 码元列表（与 JS 字符串下标一一对应）。"""
    b = text.encode("utf-16-le")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def u16_len(text: str) -> int:
    """字符串的 UTF-16 码元长度（等于 JS 的 string.length）。"""
    return len(text.encode("utf-16-le")) // 2


def _is_low_surrogate(u: int) -> bool:
    return 0xDC00 <= u <= 0xDFFF


def _expand_to_code_point(units: list[int], start: int, end: int) -> tuple[int, int]:
    """若边界落在代理对中间，向外扩展，绝不拆散一个完整字符（如表情）。"""
    n = len(units)
    if 0 < start < n and _is_low_surrogate(units[start]):
        start -= 1
    if 0 < end < n and _is_low_surrogate(units[end]):
        end += 1
    return start, end


def _units_to_str(units: list[int]) -> str:
    b = bytearray()
    for u in units:
        b += bytes((u & 0xFF, (u >> 8) & 0xFF))
    return bytes(b).decode("utf-16-le")


def mask_text(content: str, ranges: list[tuple[int, int]]) -> str:
    """按给定区间（UTF-16 码元偏移）的并集遮盖正文，返回遮盖后的文本。"""
    units = u16_units(content)
    n = len(units)
    masked = bytearray(n)
    for start, end in ranges:
        start, end = _expand_to_code_point(units, start, end)
        for i in range(max(0, start), min(n, end)):
            masked[i] = 1
    out = [MASK_CHAR if m else u for u, m in zip(units, masked)]
    return _units_to_str(out)


def excerpt(content: str, start: int, end: int, limit: int = 40) -> str:
    """截取选区原文作为摘要（边界同样按完整字符扩展）。"""
    units = u16_units(content)
    start, end = _expand_to_code_point(units, start, end)
    text = _units_to_str(units[start:end])
    return text if len(text) <= limit else text[:limit] + "…"
