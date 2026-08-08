"""
移动运营商集成 - 常量定义模块

本模块集中定义集成中使用的所有常量与工具函数，包括：
- 集成域名、配置项键名
- 运营商类型与显示名称
- 手机号脱敏、配置项/设备名称格式化等工具函数

设计原则：按中国信息模式统一处理，如手机号脱敏（前3后4）、设备/配置项命名格式。
"""

import re

# ========== 集成标识 ==========
# 集成在 Home Assistant 中的唯一域名，用于 manifest、配置项、实体 ID 等
DOMAIN = "ha_china_comm"

# ========== 配置条目类型 ==========
# 配置项中用于区分运营商类型的键名
CONF_ENTRY_TYPE = "entry_type"
# 中国电信类型标识
ENTRY_TYPE_TELECOM = "telecom"
# 中国联通类型标识
ENTRY_TYPE_UNICOM = "unicom"

# ========== 中国电信相关配置项 ==========
CONF_PHONENUM = "phonenum"      # 手机号码（11 位）
CONF_PASSWORD = "password"      # 电信服务密码（用于登录掌上营业厅）
CONF_DEVICE_ID = "device_id"    # 设备唯一标识，用于区分同一手机号的多设备场景

# ========== 中国联通相关配置项 ==========
CONF_OPENID = "openid"                          # 联通小程序 OpenID，用于 API 鉴权
CONF_REFRESH_INTERVAL = "refresh_interval"      # 数据刷新间隔（分钟）

# ========== 中国电信额外配置项 ==========
CONF_TELECOM_DEVICE_ID = "telecom_device_id"    # 电信设备信任 ID（用于设备绑定授权）
CONF_LOGIN_INFO = "login_info"                  # 缓存的登录信息（token 等）
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"  # 更新间隔（分钟）

# ========== 中国电信设备信任ID默认值 ==========
DEFAULT_TELECOM_DEVICE_ID = "a2d144f14cf32845"  # 默认设备信任ID

# ========== 默认值与限制 ==========
DEFAULT_UPDATE_INTERVAL_MINUTES = 360           # 默认更新间隔（分钟）
MIN_UPDATE_INTERVAL_MINUTES = 5                 # 最小更新间隔（分钟）
LOGIN_RETRY_COOLDOWN_MINUTES = 60               # 登录重试冷却时间（分钟）

# ========== 运营商显示名称 ==========
# 用于配置项标题、设备名称、实体属性等处的友好显示
OPERATOR_TELECOM = "中国电信"
OPERATOR_UNICOM = "中国联通"

# ========== 实体显示名称（CT/CU 统一） ==========
ENTITY_BALANCE = "账户余额"
ENTITY_CURRENT_MONTH_COST = "本月消费"
ENTITY_ARREAR = "欠费"
ENTITY_CREDIT = "信用额度"
ENTITY_FLOW_TOTAL = "流量总量"
ENTITY_FLOW_USE = "流量已用"
ENTITY_FLOW_BALANCE = "流量剩余"
ENTITY_FLOW_OVER = "流量超出"
ENTITY_FLOW_PERCENT = "流量使用率"
ENTITY_VOICE_TOTAL = "通话总量"
ENTITY_VOICE_USE = "通话已用"
ENTITY_VOICE_BALANCE = "通话剩余"
ENTITY_VOICE_PERCENT = "通话使用率"
ENTITY_SMS_TOTAL = "短信总量"
ENTITY_SMS_USE = "短信已用"
ENTITY_SMS_BALANCE = "短信剩余"
ENTITY_SMS_PERCENT = "短信使用率"
ENTITY_POINTS = "积分"
ENTITY_LAST_REFRESH = "最近刷新时间"


def mask_phone_number(phonenum):
    """
    中国信息模式：手机号脱敏，前3位 + **** + 后4位。

    用于在 UI、日志、设备名称等场景中保护用户隐私，
    例如：13812345678 -> 138****5678

    Args:
        phonenum: 原始手机号字符串

    Returns:
        脱敏后的字符串，非 11 位数字时返回原值或截断脱敏
    """
    if not phonenum or not isinstance(phonenum, str):
        return ""
    phonenum = phonenum.strip()
    # 标准 11 位手机号：前3 + **** + 后4
    if re.match(r"^\d{11}$", phonenum):
        return f"{phonenum[:3]}****{phonenum[7:]}"
    # 非标准格式：尽量脱敏中间部分
    return phonenum if len(phonenum) <= 11 else f"{phonenum[:3]}****{phonenum[-4:]}"


def format_entry_title(operator, phonenum_or_name):
    """
    统一配置项标题格式：{运营商} {脱敏号或名称}。

    用于在 Home Assistant 配置项列表中显示，便于用户区分不同账号。
    若 phonenum_or_name 为 11 位数字则脱敏，否则直接使用（如自定义名称）。

    Args:
        operator: 运营商显示名，如 "中国电信"
        phonenum_or_name: 手机号或自定义名称

    Returns:
        格式化后的标题字符串
    """
    display = mask_phone_number(phonenum_or_name) if phonenum_or_name and re.match(r"^\d{11}$", str(phonenum_or_name).strip()) else (phonenum_or_name or "套餐")
    return f"{operator} {display}"


def format_device_name(operator, phonenum_or_name):
    """
    统一设备名称格式：{运营商} {脱敏号或名称} 套餐信息。

    用于在设备注册表中显示，与 format_entry_title 类似，但后缀为「套餐信息」。

    Args:
        operator: 运营商显示名
        phonenum_or_name: 手机号或自定义名称

    Returns:
        格式化后的设备名称
    """
    display = mask_phone_number(phonenum_or_name) if phonenum_or_name and re.match(r"^\d{11}$", str(phonenum_or_name).strip()) else (phonenum_or_name or "套餐")
    return f"{operator} {display} 套餐信息"


# ========== 实体标识符格式 ==========
# 格式：{运营商前缀}_{手机号后4位}_{传感器类型}
# 示例：ct_7435_balance（电信）、cu_7435_voice（联通）
def format_entity_id(operator_prefix, phonenum_last4, sensor_key):
    """
    生成实体 object_id，用于 suggested_object_id。

    最终 entity_id 为 sensor.{返回值}，如 sensor.ct_7435_balance。
    运营商前缀：ct=电信、cu=联通。

    Args:
        operator_prefix: 运营商前缀（ct/cu）
        phonenum_last4: 手机号后 4 位或 entry_id 前 4 位
        sensor_key: 传感器类型键名（小写无下划线，如 balance、flowtotal）

    Returns:
        格式化的 object_id 字符串
    """
    return f"{operator_prefix}_{phonenum_last4}_{sensor_key}"
