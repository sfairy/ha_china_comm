"""
移动运营商集成 - 工具函数模块

本模块提供 CT/CU 共用的解析与转换逻辑。

主要函数：
- convert_flow：流量字符串或数值转指定单位（中国电信 API 使用）
"""


def convert_flow(size_str, target_unit="KB", decimal=0):
    """
    将流量字符串（如 "1.5GB"）或数值转换为指定单位的数值。

    字符串格式：数值 + 单位，单位取最后 2 字符（GB/MB/KB/TB），如 "1.5GB"、"1024MB"。
    中国电信 API 通常返回 KB 数值或 "1024MB" 格式，本函数用于统一转换为 GB 等。

    Args:
        size_str: 流量字符串（如 "1024MB"）或数值（默认 KB）
        target_unit: 目标单位，KB/MB/GB/TB
        decimal: 小数位数，0 表示取整

    Returns:
        转换后的数值，无效输入返回 0
    """
    unit_dict = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    if not size_str:
        return 0
    if isinstance(size_str, str):
        # 格式：数值 + 单位，单位取最后 2 字符（GB/MB/KB/TB）
        size, unit = float(size_str[:-2]), size_str[-2:]
    elif isinstance(size_str, (int, float)):
        size, unit = size_str, "KB"
    else:
        return 0
    if unit in unit_dict and target_unit in unit_dict:
        if decimal == 0:
            return int(size * unit_dict[unit] / unit_dict[target_unit])
        return round(size * unit_dict[unit] / unit_dict[target_unit], decimal)
    return 0
