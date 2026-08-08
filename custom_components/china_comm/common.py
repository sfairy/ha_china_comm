"""
移动运营商集成 - 公共模块

本模块集中中国电信 CT、中国联通 CU 的公共逻辑，
减少重复代码，便于后期维护与扩展。

主要功能：
- device_info：构建设备信息，用于将实体分组显示在设备下
- ensure_short_entity_id：统一 entity_id 格式为 sensor.{运营商}_{后4位}_{类型}
- ensure_device_id：生成并持久化设备唯一标识
- get_phonenum_last4：计算手机号后 4 位，用于实体 unique_id 前缀
- BaseOperatorSingleSensor：单项传感器基类，供 CT key 型传感器继承
"""

import logging
import uuid

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, CONF_DEVICE_ID

# 模块级日志记录器，供 ensure_short_entity_id 等函数使用
_LOGGER = logging.getLogger(__name__)


def device_info(entry_id: str, device_name: str, manufacturer: str) -> DeviceInfo:
    """
    构建设备信息，用于将实体分组显示在设备下。

    在 Home Assistant 中，同一设备下的多个传感器实体通过 device_info 关联，
    便于在设备视图中统一展示。各运营商均使用相同结构，仅 manufacturer 不同。

    Args:
        entry_id: 配置项唯一 ID，用于 identifiers
        device_name: 设备显示名称（如「中国电信 138****5678 套餐信息」）
        manufacturer: 运营商名称（OPERATOR_TELECOM / OPERATOR_UNICOM）

    Returns:
        DeviceInfo 实例，供 SensorEntity.device_info 使用
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=device_name,
        manufacturer=manufacturer,
    )


def ensure_short_entity_id(hass, entity_id: str, desired_object_id: str, logger=None):
    """
    若 entity_id 与期望不符，通过 entity_registry 更新为短格式。

    Home Assistant 首次创建实体时可能生成较长 entity_id（含 UUID 等），
    本函数在实体加入时将其更新为统一格式，如 sensor.ct_7435_balance。
    若目标 entity_id 已存在（如重复配置），则记录警告并跳过。

    Args:
        hass: Home Assistant 核心实例
        entity_id: 当前实体的完整 entity_id（如 sensor.ct_xxx_balance_abc123）
        desired_object_id: 期望的 object_id，不含 sensor. 前缀（如 ct_7435_balance）
        logger: 可选日志记录器，默认使用本模块 _LOGGER
    """
    log = logger or _LOGGER
    desired_id = f"sensor.{desired_object_id}"
    if entity_id != desired_id:
        registry = er.async_get(hass)
        try:
            registry.async_update_entity(entity_id, new_entity_id=desired_id)
        except ValueError:
            log.warning("无法将实体 %s 更新为 %s，可能已存在", entity_id, desired_id)


def ensure_device_id(hass, entry) -> str:
    """
    若配置项中无 device_id，则生成 UUID 并持久化到配置项，返回 device_id。

    用于设备注册表，区分同一手机号在多设备场景下的不同实例。
    CT/CU 在 async_setup 时均需调用此函数，确保 device_id 存在。

    Args:
        hass: Home Assistant 核心实例
        entry: 当前配置项（ConfigEntry）

    Returns:
        device_id 字符串，已存在则直接返回，不存在则生成并更新 entry 后返回
    """
    if CONF_DEVICE_ID not in entry.data:
        device_id = str(uuid.uuid4())
        new_data = {**entry.data, CONF_DEVICE_ID: device_id}
        hass.config_entries.async_update_entry(entry, data=new_data)
    else:
        device_id = entry.data[CONF_DEVICE_ID]
    return device_id


def get_phonenum_last4(phonenum: str, entry_id: str) -> str:
    """
    获取手机号后 4 位，用于实体 unique_id 和 entity_id 的前缀。

    实体标识符格式为 {运营商}_{phonenum_last4}_{类型}，如 CT_7435_balance。
    若手机号不足 4 位或为空（如联通仅填 OpenID 时），则使用 entry_id 前 4 位兜底。

    Args:
        phonenum: 手机号字符串，可为空
        entry_id: 配置项唯一 ID

    Returns:
        4 位字符串，用于 unique_id 和 suggested_object_id
    """
    return phonenum[-4:] if phonenum and len(phonenum) >= 4 else entry_id[:4]


from homeassistant.components.sensor import SensorEntity

from .const import format_entity_id


class BaseOperatorSingleSensor(SensorEntity):
    """
    运营商单项传感器基类，供 CT 的 key 型传感器继承。

    数据来源：coordinator.data[key]，由 DataUpdateCoordinator 定期更新。
    子类仅需在 __init__ 中调用 super().__init__(..., operator_prefix, manufacturer)，
    即可复用 state、device_info、async_added_to_hass、async_update 等逻辑。

    注意：联通 CU 因 API 结构不同，使用独立的 _UnicomBaseSensor，不继承此类。
    """

    def __init__(
        self,
        coordinator,
        device_name: str,
        key: str,
        name: str,
        unit: str,
        icon: str,
        entry_id: str,
        device_id: str,
        phonenum_last4: str,
        operator_prefix: str,
        manufacturer: str,
        entity_suffix: str = None,
    ):
        """初始化单项传感器，保存 coordinator、key、运营商前缀等。"""
        self.coordinator = coordinator
        self._device_name = device_name
        self.key = key  # coordinator.data 中的键名，如 flowTotal、voiceUsage
        self._name = name
        self._unit = unit
        self._icon = icon
        self._entry_id = entry_id
        self._device_id = device_id
        self._phonenum_last4 = phonenum_last4
        self._operator_prefix = operator_prefix
        self._manufacturer = manufacturer
        suffix = entity_suffix or key.lower()
        self._unique_id = f"{operator_prefix}_{phonenum_last4}_{suffix}"
        self._attr_suggested_object_id = format_entity_id(
            operator_prefix.lower(), phonenum_last4, suffix
        )
        self._attr_has_entity_name = True

    @property
    def name(self):
        """实体显示名称，如「流量总量」「本月消费」。"""
        return self._name

    @property
    def state(self):
        """从 coordinator.data[key] 读取当前值，无数据时返回 None。"""
        if self.coordinator.data and self.key in self.coordinator.data:
            return self.coordinator.data.get(self.key)
        return None

    @property
    def unit_of_measurement(self):
        return self._unit

    @property
    def icon(self):
        return self._icon

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self.coordinator.last_update_success

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def device_info(self):
        return device_info(self._entry_id, self._device_name, self._manufacturer)

    async def async_added_to_hass(self):
        """实体加入 HA 时：统一 entity_id 格式，并注册 coordinator 更新监听。"""
        ensure_short_entity_id(
            self.hass,
            self.entity_id,
            self._attr_suggested_object_id,
        )
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_update(self):
        """手动刷新时，请求 coordinator 重新拉取数据。"""
        await self.coordinator.async_request_refresh()
