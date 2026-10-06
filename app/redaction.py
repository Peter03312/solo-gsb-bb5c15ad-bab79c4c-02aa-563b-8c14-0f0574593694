"""基于 Unicode 码位的遮盖计算。

所有选区索引（start/end）均以 **Unicode 码位** 为单位，即 Python str 的下标，
对 emoji（包括组合字符、ZWJ 序列、肤色修饰符等代理对）天然正确。
"""
import hashlib

MASK_CHAR = "█"


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠/相邻区间。输入为 (start, end) 半开区间。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def apply_mask(text: str, ranges: list[tuple[int, int]]) -> str:
    """按并集遮盖：每段连续被遮盖的码位区间替换为一个 █，其余原样保留。"""
    merged = merge_ranges(ranges)
    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        parts.append(MASK_CHAR)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def preview_hash(text: str, ranges: list[tuple[int, int]]) -> str:
    """最终预览内容的稳定指纹：确认与发布必须指向同一份预览。"""
    return hashlib.sha256(apply_mask(text, ranges).encode("utf-8")).hexdigest()
