"""
移动运营商集成 - 主入口模块

本模块是 Home Assistant 移动运营商集成的入口点，负责：

1. 集成加载：当用户通过 UI 添加配置项时，将配置数据存入 hass.data[DOMAIN]
2. 平台转发：根据配置项类型（电信/联通）创建对应的传感器实体
3. 集成卸载：删除配置项时，卸载关联实体并清理 hass.data

支持的运营商：中国电信(CT)、中国联通(CU)。
各运营商独立封装，支持同一用户配置多个账号。
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# 日志记录器，用于输出集成运行时的调试与错误信息
_LOGGER = logging.getLogger(__name__)

# 本集成启用的平台列表，目前仅包含传感器平台（用于展示套餐、流量、余额等数据）
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    从配置项加载移动运营商集成。

    当用户通过 UI 添加新的移动运营商配置项时，Home Assistant 会调用此函数。
    本函数会：
    1. 将配置数据存入 hass.data[DOMAIN]，供各平台组件访问
    2. 转发到 sensor 平台，根据条目类型（电信/联通/移动/广电）创建对应的传感器实体

    Args:
        hass: Home Assistant 核心实例
        entry: 用户添加的配置项，包含手机号、密码、OpenID 等

    Returns:
        True 表示集成加载成功
    """
    # 确保 DOMAIN 在 hass.data 中已初始化
    hass.data.setdefault(DOMAIN, {})
    # 以 entry_id 为键存储该配置项的数据，便于 sensor 平台按 entry 区分不同设备
    hass.data[DOMAIN][entry.entry_id] = entry.data

    # 将配置项转发给 PLATFORMS 中的各平台进行初始化（此处为 sensor）
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    卸载配置项时调用。

    当用户删除某个移动运营商配置项时，Home Assistant 会调用此函数。
    本函数会：
    1. 卸载该配置项关联的所有平台实体（传感器等）
    2. 从 hass.data 中清除该配置项的数据

    Args:
        hass: Home Assistant 核心实例
        entry: 待卸载的配置项

    Returns:
        True 表示卸载成功，False 表示卸载过程中出现错误
    """
    # 尝试卸载该配置项关联的所有平台
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # 卸载成功后，从 hass.data 中移除该配置项的数据
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
