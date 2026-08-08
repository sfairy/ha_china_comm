"""
中国联通 - 传感器平台模块

本模块负责中国联通套餐数据的拉取与展示：

功能概述：
- 通过联通小程序 API 获取语音、短信、流量及账户余额数据
- 使用 DataUpdateCoordinator 按配置的刷新间隔定期拉取数据
- 账户余额为聚合实体（主状态=可用余额-欠费，欠费时负值；attributes 镜像全部单项数据含套餐明细）
- 其余为单值实体：本月消费、信用额度、欠费、流量合计/通用/专属/其他、上月转接、通话/短信

鉴权方式：OpenID（从联通小程序获取），无需服务密码。
"""

import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.core import callback

from ..common import (
    device_info,
    ensure_device_id,
    ensure_short_entity_id,
    get_phonenum_last4,
)
from ..const import (
    CONF_OPENID,
    CONF_REFRESH_INTERVAL,
    CONF_PHONENUM,
    OPERATOR_UNICOM,
    ENTITY_BALANCE,
    ENTITY_CURRENT_MONTH_COST,
    ENTITY_ARREAR,
    ENTITY_CREDIT,
    ENTITY_FLOW_TOTAL,
    ENTITY_FLOW_USE,
    ENTITY_FLOW_BALANCE,
    ENTITY_FLOW_OVER,
    ENTITY_FLOW_PERCENT,
    ENTITY_VOICE_TOTAL,
    ENTITY_VOICE_USE,
    ENTITY_VOICE_BALANCE,
    ENTITY_VOICE_PERCENT,
    ENTITY_SMS_TOTAL,
    ENTITY_SMS_USE,
    ENTITY_SMS_BALANCE,
    ENTITY_SMS_PERCENT,
    ENTITY_LAST_REFRESH,
    format_entity_id,
    format_device_name,
)
from .api import ChinaUnicom

_LOGGER = logging.getLogger(__name__)

# 流量分类显示名（与 CT 通用/专用命名风格一致；对应 API flowType）
ENTITY_FLOW_CARRIED = "上月转接流量"
ENTITY_COMMON_FLOW_TOTAL = "通用流量总量"
ENTITY_COMMON_FLOW_USE = "通用流量已用"
ENTITY_COMMON_FLOW_BALANCE = "通用流量剩余"
ENTITY_SPECIAL_FLOW_TOTAL = "专用流量总量"
ENTITY_SPECIAL_FLOW_USE = "专用流量已用"
ENTITY_SPECIAL_FLOW_BALANCE = "专用流量剩余"
ENTITY_OTHER_FLOW_TOTAL = "其他流量总量"
ENTITY_OTHER_FLOW_USE = "其他流量已用"
ENTITY_OTHER_FLOW_BALANCE = "其他流量剩余"


def _parse_mb(value: str | float | None) -> float:
    """解析 MB 值。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).upper().replace("MB", "").replace("GB", "").strip()
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _valid_data_items(data_items: list) -> list:
    """过滤有效流量项：排除不限量（total=0）。"""
    return [item for item in data_items if float(item.get("total") or 0) > 0]


def _sum_field_mb(items: list, field: str, require_value: bool = False) -> float:
    """对流量项某字段求和（MB）。"""
    total = 0.0
    for item in items:
        val = item.get(field)
        if require_value and not val:
            continue
        total += _parse_mb(val)
    return total


def _sum_by_flow_type(
    data_items: list,
    flow_type: str,
    field: str,
    *,
    skip_unlimited: bool = False,
    require_value: bool = False,
) -> float:
    """按 flowType 汇总字段（MB）。flowType: 1通用 2专属 3其他。"""
    items = [i for i in data_items if i.get("flowType") == flow_type]
    if skip_unlimited:
        items = _valid_data_items(items)
    return _sum_field_mb(items, field, require_value=require_value)


def _mb_to_gb(mb: float) -> float:
    """MB 转 GB，保留两位小数。"""
    return round(mb / 1024, 2)


def _safe_num(value, default=0, as_int: bool = False):
    """安全转数值。"""
    try:
        n = float(value if value is not None else default)
        return int(n) if as_int else round(n, 2)
    except (ValueError, TypeError):
        return default if not as_int else int(default or 0)


def _build_unicom_balance_attributes(data: dict | None) -> dict:
    """构建账户余额综合实体 attributes（镜像本设备全部单项实体数据）。

    字段对齐 CT 综合实体风格，并补充联通特有项（信用额度、上月结余、分类流量、上月转接）。
    """
    if not data:
        return {}

    balance_detail = data.get("balance_detail", {}) or {}
    usage_details = data.get("usage_details", {}) or {}
    bill_detail = data.get("bill_detail", {}) or {}

    available = balance_detail.get("canusefeecustNew") or balance_detail.get("canusefeecust")
    month_cost = bill_detail.get("realPayFee")
    if month_cost is None:
        month_cost = balance_detail.get("totalrealfee") or balance_detail.get("realfeecustnew")

    attrs = {
        # —— 账户 ——
        "账户余额": _safe_num(available),
        "本月消费": _safe_num(month_cost),
        "欠费": _safe_num(balance_detail.get("allbowefeecust")),
    }

    # —— 通话 ——
    voice = usage_details.get("voice") or {}
    voice_total = _safe_num(voice.get("total"), as_int=True)
    voice_use = _safe_num(voice.get("use"), as_int=True)
    voice_remain = _safe_num(voice.get("remain"), as_int=True)
    try:
        voice_ratio = round(float(voice.get("usedPercent", 0)), 2)
    except (ValueError, TypeError):
        voice_ratio = round(voice_use / voice_total * 100, 2) if voice_total > 0 else 0
    attrs.update({
        "通话总量": voice_total,
        "通话已用": voice_use,
        "通话剩余": voice_remain,
        "通话使用率": voice_ratio,
    })

    # —— 短信 ——
    sms = usage_details.get("sms") or {}
    sms_total = _safe_num(sms.get("total"), as_int=True)
    sms_use = _safe_num(sms.get("use"), as_int=True)
    sms_remain = _safe_num(sms.get("remain"), as_int=True)
    sms_ratio = round(sms_use / sms_total * 100, 2) if sms_total > 0 else 0
    attrs.update({
        "短信总量": sms_total,
        "短信已用": sms_use,
        "短信剩余": sms_remain,
        "短信使用率": sms_ratio,
    })

    # —— 流量合计（排除不限量）——
    data_items = usage_details.get("data_items") or []
    valid = _valid_data_items(data_items)
    total_use = _sum_field_mb(valid, "use")
    total_total = _sum_field_mb(valid, "total")
    total_remain = _sum_field_mb(valid, "remain", require_value=True)
    total_exceed = _sum_field_mb(valid, "xexceedvalue", require_value=True)
    carried = _sum_field_mb(data_items, "beforeTotal", require_value=True)
    flow_ratio = round(total_use / total_total * 100, 2) if total_total > 0 else 0

    attrs.update({
        "流量总量": _mb_to_gb(total_total),
        "流量已用": _mb_to_gb(total_use),
        "流量剩余": _mb_to_gb(total_remain),
        "流量超出": _mb_to_gb(total_exceed),
        "流量使用率": flow_ratio,
        # —— 通用 flowType=1 ——
        "通用流量总量": _mb_to_gb(_sum_by_flow_type(data_items, "1", "total")),
        "通用流量已用": _mb_to_gb(_sum_by_flow_type(data_items, "1", "use")),
        "通用流量剩余": _mb_to_gb(
            _sum_by_flow_type(data_items, "1", "remain", require_value=True)
        ),
        # —— 专用 flowType=2（排除不限量）——
        "专用流量总量": _mb_to_gb(
            _sum_by_flow_type(data_items, "2", "total", skip_unlimited=True)
        ),
        "专用流量已用": _mb_to_gb(
            _sum_by_flow_type(data_items, "2", "use", skip_unlimited=True)
        ),
        "专用流量剩余": _mb_to_gb(
            _sum_by_flow_type(
                data_items, "2", "remain", skip_unlimited=True, require_value=True
            )
        ),
    })

    attrs["最近刷新时间"] = data.get("last_refresh_time")
    return attrs


def _calc_balance_state(data: dict | None):
    """主状态 = 可用余额 - 欠费，欠费时为负值。"""
    if not data:
        return None
    balance_detail = data.get("balance_detail", {}) or {}
    try:
        cur = float(
            balance_detail.get("canusefeecustNew")
            or balance_detail.get("canusefeecust")
            or 0
        )
        owed = float(balance_detail.get("allbowefeecust") or 0)
        return round(cur - owed, 2)
    except (ValueError, TypeError):
        return None


async def async_setup_unicom(hass, entry, async_add_entities):
    """设置中国联通传感器。"""
    openid = entry.data[CONF_OPENID]
    name = entry.data.get("name", "中国联通")
    refresh_interval = entry.data.get(CONF_REFRESH_INTERVAL, 15)
    phonenum = entry.data.get(CONF_PHONENUM, "")
    entry_id = entry.entry_id

    device_id = ensure_device_id(hass, entry)
    phonenum_last4 = get_phonenum_last4(phonenum, entry_id)
    device_name = format_device_name(OPERATOR_UNICOM, phonenum or (name if name != "中国联通" else "套餐"))

    scan_interval_td = timedelta(minutes=refresh_interval)
    session = async_get_clientsession(hass)

    coordinator = ChinaUnicomDataUpdateCoordinator(
        hass, session, openid, entry_id, scan_interval_td
    )

    await coordinator.async_config_entry_first_refresh()

    # 获取短信总量用于决定是否创建短信传感器
    sms_total = _get_sms_total(coordinator.data)

    # 创建传感器实体（格式与既有 CU 实体一致；补充欠费/分类流量/上月转接）
    entities = [
        ChinaUnicomBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomRealFeeNewSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomCreditValueSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomArrearSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomLastRefreshSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        # 流量合计
        ChinaUnicomFlowTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomFlowUseSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomFlowBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomFlowOverSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomFlowUsageRatioSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomFlowCarriedSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        # 通用流量 (flowType=1)
        ChinaUnicomCommonFlowTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomCommonFlowUseSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomCommonFlowBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        # 专用流量 (flowType=2，排除不限量)
        ChinaUnicomSpecialFlowTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomSpecialFlowUseSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomSpecialFlowBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        # 其他流量 (flowType=3)
        ChinaUnicomOtherFlowTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomOtherFlowUseSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomOtherFlowBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        # 通话
        ChinaUnicomVoiceTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomVoiceUsageSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomVoiceAvailableSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomVoiceUsageRatioSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaUnicomSmsTotalSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
    ]
    if sms_total > 0:
        entities.extend([
            ChinaUnicomSmsUsageSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
            ChinaUnicomSmsAvailableSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
            ChinaUnicomSmsUsageRatioSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ])

    async_add_entities(entities)


def _get_sms_total(data: dict) -> int:
    """从数据中提取短信总量。"""
    usage_details = data.get("usage_details", {})
    sms = usage_details.get("sms", {})
    return int(float(sms.get("total", 0)))


class ChinaUnicomDataUpdateCoordinator(DataUpdateCoordinator):
    """中国联通数据更新协调器。"""

    def __init__(self, hass, session, openid, entry_id, update_interval):
        """初始化协调器。"""
        self.openid = openid
        self.entry_id = entry_id
        self.session = session
        super().__init__(hass, _LOGGER, name="China Unicom Data", update_interval=update_interval)

    async def _async_update_data(self):
        """执行数据更新。"""
        try:
            client = ChinaUnicom(self.openid)
            return await client.fetch_all(self.session, timeout=15)
        except Exception as err:
            raise UpdateFailed(f"与 API 通信失败: {err}") from err


class ChinaUnicomBalanceSensor(SensorEntity):
    """中国联通账户余额综合信息实体（聚合实体）。

    主状态：可用余额 - 欠费，欠费时显示负值。
    attributes：镜像本设备全部单项实体（账户/流量合计与分类/通话/短信/套餐明细）。
    """

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        self.coordinator = coordinator
        self._device_name = device_name
        self._entry_id = entry_id
        self._device_id = device_id
        self._phonenum_last4 = phonenum_last4
        self._attr_suggested_object_id = format_entity_id("cu", phonenum_last4, "balance")
        self._attr_has_entity_name = True

    @property
    def name(self):
        return ENTITY_BALANCE

    @property
    def icon(self):
        return "mdi:cash"

    @property
    def unique_id(self):
        return f"CU_{self._phonenum_last4}_balance"

    @property
    def state(self):
        """主状态 = 可用余额 - 欠费，欠费时显示负值。"""
        return _calc_balance_state(self.coordinator.data)

    @property
    def unit_of_measurement(self):
        return "元"

    @property
    def extra_state_attributes(self):
        """包含本设备所有实体的数据。"""
        return _build_unicom_balance_attributes(self.coordinator.data)

    @property
    def device_info(self):
        return device_info(self._entry_id, self._device_name, OPERATOR_UNICOM)

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self.coordinator.last_update_success

    async def async_added_to_hass(self):
        ensure_short_entity_id(
            self.hass, self.entity_id, format_entity_id("cu", self._phonenum_last4, "balance"),
        )
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )


class _UnicomBaseSensor(SensorEntity):
    """联通独立传感器基类。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4, name_key, unit, unique_suffix):
        self.coordinator = coordinator
        self._device_name = device_name
        self._entry_id = entry_id
        self._device_id = device_id
        self._phonenum_last4 = phonenum_last4
        self._name_key = name_key
        self._unit = unit
        self._unique_suffix = unique_suffix
        self._state = 0
        self._attr_suggested_object_id = format_entity_id("cu", phonenum_last4, unique_suffix)
        self._attr_has_entity_name = True

    @property
    def name(self):
        return self._name_key

    @property
    def unique_id(self):
        return f"CU_{self._phonenum_last4}_{self._unique_suffix}"

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return self._unit

    @property
    def device_info(self):
        return device_info(self._entry_id, self._device_name, OPERATOR_UNICOM)

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self.coordinator.last_update_success

    async def async_added_to_hass(self):
        ensure_short_entity_id(
            self.hass,
            self.entity_id,
            format_entity_id("cu", self._phonenum_last4, self._unique_suffix),
        )
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator_update))
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self):
        raise NotImplementedError


# ========== 流量传感器 ==========
class ChinaUnicomFlowTotalSensor(_UnicomBaseSensor):
    """流量总量传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_FLOW_TOTAL, "GB", "flowtotal")

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return

        valid = _valid_data_items(data.get("usage_details", {}).get("data_items", []))
        self._state = _mb_to_gb(_sum_field_mb(valid, "total"))
        self.async_write_ha_state()


class ChinaUnicomFlowUseSensor(_UnicomBaseSensor):
    """流量已用传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_FLOW_USE, "GB", "flowuse")

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return

        valid = _valid_data_items(data.get("usage_details", {}).get("data_items", []))
        self._state = _mb_to_gb(_sum_field_mb(valid, "use"))
        self.async_write_ha_state()


class ChinaUnicomFlowBalanceSensor(_UnicomBaseSensor):
    """流量剩余传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_FLOW_BALANCE, "GB", "flowbalance")

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return

        valid = _valid_data_items(data.get("usage_details", {}).get("data_items", []))
        self._state = _mb_to_gb(_sum_field_mb(valid, "remain", require_value=True))
        self.async_write_ha_state()


class ChinaUnicomFlowOverSensor(_UnicomBaseSensor):
    """流量超出传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_FLOW_OVER, "GB", "flowover")

    @property
    def icon(self):
        return "mdi:network-off"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return

        valid = _valid_data_items(data.get("usage_details", {}).get("data_items", []))
        self._state = _mb_to_gb(_sum_field_mb(valid, "xexceedvalue", require_value=True))
        self.async_write_ha_state()


class ChinaUnicomFlowUsageRatioSensor(_UnicomBaseSensor):
    """流量使用率传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_FLOW_PERCENT, "%", "flowpercent")

    @property
    def icon(self):
        return "mdi:percent"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return

        valid = _valid_data_items(data.get("usage_details", {}).get("data_items", []))
        use_mb = _sum_field_mb(valid, "use")
        total_mb = _sum_field_mb(valid, "total")
        self._state = round(use_mb / total_mb * 100, 2) if total_mb > 0 else 0
        self.async_write_ha_state()


class ChinaUnicomFlowCarriedSensor(_UnicomBaseSensor):
    """上月转接流量传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_FLOW_CARRIED, "GB", "flowcarried",
        )

    @property
    def icon(self):
        return "mdi:arrow-left-bold"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(_sum_field_mb(items, "beforeTotal", require_value=True))
        self.async_write_ha_state()


class ChinaUnicomCommonFlowTotalSensor(_UnicomBaseSensor):
    """通用流量总量传感器（flowType=1）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_COMMON_FLOW_TOTAL, "GB", "commontotal",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(_sum_by_flow_type(items, "1", "total"))
        self.async_write_ha_state()


class ChinaUnicomCommonFlowUseSensor(_UnicomBaseSensor):
    """通用流量已用传感器（flowType=1）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_COMMON_FLOW_USE, "GB", "commonuse",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(_sum_by_flow_type(items, "1", "use"))
        self.async_write_ha_state()


class ChinaUnicomCommonFlowBalanceSensor(_UnicomBaseSensor):
    """通用流量剩余传感器（flowType=1）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_COMMON_FLOW_BALANCE, "GB", "commonbalance",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(
            _sum_by_flow_type(items, "1", "remain", require_value=True)
        )
        self.async_write_ha_state()


class ChinaUnicomSpecialFlowTotalSensor(_UnicomBaseSensor):
    """专用流量总量传感器（flowType=2，排除不限量）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_SPECIAL_FLOW_TOTAL, "GB", "specialtotal",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(
            _sum_by_flow_type(items, "2", "total", skip_unlimited=True)
        )
        self.async_write_ha_state()


class ChinaUnicomSpecialFlowUseSensor(_UnicomBaseSensor):
    """专用流量已用传感器（flowType=2，排除不限量）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_SPECIAL_FLOW_USE, "GB", "specialuse",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(
            _sum_by_flow_type(items, "2", "use", skip_unlimited=True)
        )
        self.async_write_ha_state()


class ChinaUnicomSpecialFlowBalanceSensor(_UnicomBaseSensor):
    """专用流量剩余传感器（flowType=2，排除不限量）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_SPECIAL_FLOW_BALANCE, "GB", "specialbalance",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(
            _sum_by_flow_type(
                items, "2", "remain", skip_unlimited=True, require_value=True
            )
        )
        self.async_write_ha_state()


class ChinaUnicomOtherFlowTotalSensor(_UnicomBaseSensor):
    """其他流量总量传感器（flowType=3）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_OTHER_FLOW_TOTAL, "GB", "othertotal",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(_sum_by_flow_type(items, "3", "total"))
        self.async_write_ha_state()


class ChinaUnicomOtherFlowUseSensor(_UnicomBaseSensor):
    """其他流量已用传感器（flowType=3）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_OTHER_FLOW_USE, "GB", "otheruse",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(_sum_by_flow_type(items, "3", "use"))
        self.async_write_ha_state()


class ChinaUnicomOtherFlowBalanceSensor(_UnicomBaseSensor):
    """其他流量剩余传感器（flowType=3）。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_OTHER_FLOW_BALANCE, "GB", "otherbalance",
        )

    @property
    def icon(self):
        return "mdi:network"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        items = data.get("usage_details", {}).get("data_items", [])
        self._state = _mb_to_gb(
            _sum_by_flow_type(items, "3", "remain", require_value=True)
        )
        self.async_write_ha_state()


# ========== 语音传感器 ==========
class ChinaUnicomVoiceTotalSensor(_UnicomBaseSensor):
    """语音总量传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_VOICE_TOTAL, "分钟", "voicetotal")

    @property
    def icon(self):
        return "mdi:phone"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        voice = data.get("usage_details", {}).get("voice", {})
        try:
            self._state = int(float(voice.get("total", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomVoiceUsageSensor(_UnicomBaseSensor):
    """通话已用传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_VOICE_USE, "分钟", "voiceusage")

    @property
    def icon(self):
        return "mdi:phone"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        voice = data.get("usage_details", {}).get("voice", {})
        try:
            self._state = int(float(voice.get("use", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomVoiceAvailableSensor(_UnicomBaseSensor):
    """通话剩余传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_VOICE_BALANCE, "分钟", "voicebalance")

    @property
    def icon(self):
        return "mdi:phone"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        voice = data.get("usage_details", {}).get("voice", {})
        try:
            self._state = int(float(voice.get("remain", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomVoiceUsageRatioSensor(_UnicomBaseSensor):
    """通话使用率传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_VOICE_PERCENT, "%", "voicepercent")

    @property
    def icon(self):
        return "mdi:percent"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        voice = data.get("usage_details", {}).get("voice", {})
        try:
            self._state = round(float(voice.get("usedPercent", 0)), 2)
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


# ========== 短信传感器 ==========
class ChinaUnicomSmsTotalSensor(_UnicomBaseSensor):
    """短信总量传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_SMS_TOTAL, "条", "smstotal")

    @property
    def icon(self):
        return "mdi:message"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        sms = data.get("usage_details", {}).get("sms", {})
        try:
            self._state = int(float(sms.get("total", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomSmsUsageSensor(_UnicomBaseSensor):
    """短信已用传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_SMS_USE, "条", "smsusage")

    @property
    def icon(self):
        return "mdi:message"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        sms = data.get("usage_details", {}).get("sms", {})
        try:
            self._state = int(float(sms.get("use", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomSmsAvailableSensor(_UnicomBaseSensor):
    """短信剩余传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_SMS_BALANCE, "条", "smsbalance")

    @property
    def icon(self):
        return "mdi:message"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        sms = data.get("usage_details", {}).get("sms", {})
        try:
            self._state = int(float(sms.get("remain", 0)))
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


class ChinaUnicomSmsUsageRatioSensor(_UnicomBaseSensor):
    """短信使用率传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_SMS_PERCENT, "%", "smspercent")

    @property
    def icon(self):
        return "mdi:percent"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if not data:
            return
        
        sms = data.get("usage_details", {}).get("sms", {})
        try:
            use = float(sms.get("use", 0))
            total = float(sms.get("total", 0))
            self._state = round(use / total * 100, 2) if total > 0 else 0
        except (ValueError, TypeError):
            self._state = 0
        self.async_write_ha_state()


# ========== 余额相关传感器 ==========
class ChinaUnicomCreditValueSensor(_UnicomBaseSensor):
    """信用额度传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_CREDIT, "元", "creditvalue")

    @property
    def icon(self):
        return "mdi:credit-card"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if data:
            balance_detail = data.get("balance_detail", {})
            try:
                self._state = float(balance_detail.get("canuselimitcust", "0.00"))
            except ValueError:
                self._state = 0
            self.async_write_ha_state()


class ChinaUnicomArrearSensor(_UnicomBaseSensor):
    """账户欠费传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(
            coordinator, device_name, entry_id, device_id, phonenum_last4,
            ENTITY_ARREAR, "元", "arrear",
        )

    @property
    def icon(self):
        return "mdi:cash-minus"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if data:
            balance_detail = data.get("balance_detail", {})
            try:
                self._state = float(balance_detail.get("allbowefeecust") or 0)
            except (ValueError, TypeError):
                self._state = 0
            self.async_write_ha_state()


class ChinaUnicomRealFeeNewSensor(_UnicomBaseSensor):
    """本月消费传感器。优先账单 realPayFee，回退余额接口。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_CURRENT_MONTH_COST, "元", "currentmonthcost")

    @property
    def icon(self):
        return "mdi:cash-clock"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if data:
            bill_detail = data.get("bill_detail", {})
            balance_detail = data.get("balance_detail", {})
            try:
                if bill_detail.get("realPayFee") is not None:
                    value = float(bill_detail.get("realPayFee") or 0)
                else:
                    value = float(
                        balance_detail.get("totalrealfee")
                        or balance_detail.get("realfeecustnew", "0.00")
                    )
                self._state = round(value, 2)
            except (ValueError, TypeError):
                self._state = 0
            self.async_write_ha_state()


class ChinaUnicomLastRefreshSensor(_UnicomBaseSensor):
    """最近刷新时间传感器。"""

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        super().__init__(coordinator, device_name, entry_id, device_id, phonenum_last4, ENTITY_LAST_REFRESH, "", "lastrefreshtime")

    @property
    def icon(self):
        return "mdi:clock-outline"

    @callback
    def _handle_coordinator_update(self):
        data = self.coordinator.data
        if data:
            self._state = data.get("last_refresh_time")
            self.async_write_ha_state()
