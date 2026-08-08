"""
移动运营商集成 - 传感器平台路由模块

本模块是 sensor 平台的入口。当 Home Assistant 加载移动运营商集成的传感器时，
会根据配置条目的 entry_type 将初始化工作分发到对应的子模块：

- CT (中国电信)：余额、流量、通话、积分等，鉴权方式为手机号+服务密码
- CU (中国联通)：语音、短信、流量、余额等，鉴权方式为 OpenID（小程序获取）

各运营商逻辑独立封装在 CT/CU 子目录，便于维护与扩展。
"""

from .const import (
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_TELECOM,
    ENTRY_TYPE_UNICOM,
)
from .CT import async_setup_telecom
from .CU import async_setup_unicom


async def async_setup_entry(hass, entry, async_add_entities):
    """
    根据配置条目类型分发到对应的传感器设置函数。

    当主 __init__.py 调用 async_forward_entry_setups 时，会触发此函数。
    根据 entry.data 中的 entry_type 决定调用电信/联通的设置逻辑。
    若 entry_type 未指定则默认按电信处理，否则按联通处理。

    Args:
        hass: Home Assistant 核心实例
        entry: 当前配置项，包含运营商类型及账号信息
        async_add_entities: 用于向 HA 注册传感器实体的回调函数
    """
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_TELECOM)

    if entry_type == ENTRY_TYPE_TELECOM:
        await async_setup_telecom(hass, entry, async_add_entities)
    else:
        await async_setup_unicom(hass, entry, async_add_entities)
