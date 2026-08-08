"""
中国电信 - 传感器平台模块

本模块负责中国电信套餐数据的拉取与展示：

功能概述：
- 通过电信掌上营业厅 API 登录并定期查询套餐数据（流量、语音、短信、余额、积分）
- 使用 DataUpdateCoordinator 统一管理数据更新与缓存，默认 6 小时刷新
- 创建余额（综合实体）、流量、通话、短信、积分等 Home Assistant 传感器实体
- 处理 token 过期（X201）时的自动重新登录
- 支持设备信任 ID（telecom_device_id）用于服务器设备绑定授权

鉴权方式：手机号 + 服务密码（6 位掌上营业厅密码）
实体结构：账户余额为聚合实体（主状态=余额-欠费），其余为单项实体
"""

import logging
import time
import uuid

from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..common import (
    device_info,
    ensure_device_id,
    ensure_short_entity_id,
    get_phonenum_last4,
    BaseOperatorSingleSensor,
)
from ..const import (
    DOMAIN,
    CONF_PHONENUM,
    CONF_PASSWORD,
    CONF_REFRESH_INTERVAL,
    CONF_DEVICE_ID,
    CONF_TELECOM_DEVICE_ID,
    DEFAULT_TELECOM_DEVICE_ID,
    OPERATOR_TELECOM,
    ENTITY_BALANCE,
    ENTITY_CURRENT_MONTH_COST,
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
    ENTITY_POINTS,
    ENTITY_LAST_REFRESH,
    format_entity_id,
    format_device_name,
)
from .api import Telecom

# 日志记录器，用于输出电信传感器相关调试与错误信息
_LOGGER = logging.getLogger(__name__)

# 登录重试冷却时间（分钟）
LOGIN_RETRY_COOLDOWN_MINUTES = 60


async def async_setup_telecom(hass, entry, async_add_entities):
    """
    设置中国电信传感器。

    流程：
    1. 从配置项读取手机号、密码
    2. 若缺少 device_id 则生成并持久化
    3. 创建 DataUpdateCoordinator 并执行首次刷新
    4. 若首次获取数据失败，则不创建任何实体（集成配置不会完成）
    5. 成功则创建余额、流量、通话、积分等传感器实体

    Args:
        hass: Home Assistant 核心实例
        entry: 当前配置项，包含 phonenum、password 等
        async_add_entities: 用于向 HA 注册传感器实体的回调
    """
    phonenum = entry.data[CONF_PHONENUM]
    password = entry.data[CONF_PASSWORD]
    refresh_interval = entry.data.get(CONF_REFRESH_INTERVAL, 15)
    entry_id = entry.entry_id

    # 检查配置项中是否有 device_id，如果没有则生成并保存
    if CONF_DEVICE_ID not in entry.data:
        device_id = str(uuid.uuid4())
        new_data = {**entry.data, CONF_DEVICE_ID: device_id}
        hass.config_entries.async_update_entry(entry, data=new_data)
    else:
        device_id = entry.data[CONF_DEVICE_ID]

    coordinator = ChinaTelecomDataUpdateCoordinator(hass, entry, phonenum, password, refresh_interval)
    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        _LOGGER.error("获取中国电信初始数据失败，集成将不会完成配置。")
        _LOGGER.error(f"coordinator.data: {coordinator.data}")
        return

    _LOGGER.info(f"中国电信数据获取成功，coordinator.data: {coordinator.data}")

    phonenum_last4 = get_phonenum_last4(phonenum, entry_id)
    device_name = format_device_name(OPERATOR_TELECOM, phonenum)

    # 短信总量为 0 时不创建短信已用/剩余/使用率实体
    sms_total = (coordinator.data or {}).get("smsTotal", 0) or 0

    # 余额实体为综合信息实体，欠费时显示负值（与联通一致），含本设备所有数据
    sensors = [
        ChinaTelecomBalanceSensor(coordinator, device_name, entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "currentMonthCost", ENTITY_CURRENT_MONTH_COST, "元", "mdi:cash-clock", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "flowTotal", ENTITY_FLOW_TOTAL, "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "flowUse", ENTITY_FLOW_USE, "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "flowBalance", ENTITY_FLOW_BALANCE, "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "flowOver", ENTITY_FLOW_OVER, "GB", "mdi:network-off", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "percentUsed", ENTITY_FLOW_PERCENT, "%", "mdi:percent", entry_id, device_id, phonenum_last4, "flowpercent"),
        ChinaTelecomSensor(coordinator, device_name, "voiceTotal", ENTITY_VOICE_TOTAL, "分钟", "mdi:phone", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "voiceUsage", ENTITY_VOICE_USE, "分钟", "mdi:phone", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "voiceBalance", ENTITY_VOICE_BALANCE, "分钟", "mdi:phone", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "voicePercentUsed", ENTITY_VOICE_PERCENT, "%", "mdi:percent", entry_id, device_id, phonenum_last4, "voicepercent"),
        ChinaTelecomSensor(coordinator, device_name, "smsTotal", ENTITY_SMS_TOTAL, "条", "mdi:message-text", entry_id, device_id, phonenum_last4),
        # 通用流量传感器
        ChinaTelecomSensor(coordinator, device_name, "commonTotal", "通用流量总量", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "commonUse", "通用流量已用", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "commonBalance", "通用流量剩余", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "commonOver", "通用流量超量", "GB", "mdi:network-off", entry_id, device_id, phonenum_last4),
        # 专用流量传感器
        ChinaTelecomSensor(coordinator, device_name, "specialTotal", "专用流量总量", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "specialUse", "专用流量已用", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "specialBalance", "专用流量剩余", "GB", "mdi:network", entry_id, device_id, phonenum_last4),
    ]
    if sms_total > 0:
        sensors.extend([
            ChinaTelecomSensor(coordinator, device_name, "smsUsage", ENTITY_SMS_USE, "条", "mdi:message-text", entry_id, device_id, phonenum_last4),
            ChinaTelecomSensor(coordinator, device_name, "smsBalance", ENTITY_SMS_BALANCE, "条", "mdi:message-text", entry_id, device_id, phonenum_last4),
            ChinaTelecomSensor(coordinator, device_name, "smsPercentUsed", ENTITY_SMS_PERCENT, "%", "mdi:percent", entry_id, device_id, phonenum_last4, "smspercent"),
        ])
    sensors.extend([
        ChinaTelecomSensor(coordinator, device_name, "points", ENTITY_POINTS, "分", "mdi:trophy", entry_id, device_id, phonenum_last4),
        ChinaTelecomSensor(coordinator, device_name, "lastRefreshTime", ENTITY_LAST_REFRESH, "", "mdi:clock-outline", entry_id, device_id, phonenum_last4),
    ])
    async_add_entities(sensors)


class ChinaTelecomBalanceSensor(SensorEntity):
    """
    中国电信余额综合信息实体（聚合实体）。

    主状态：当前余额 - 欠费，欠费时显示负值（与联通策略一致）。
    extra_state_attributes：包含本设备所有数据（流量、语音、短信、消费、积分、最近刷新时间等）。
    """

    def __init__(self, coordinator, device_name, entry_id, device_id, phonenum_last4):
        """初始化余额传感器，关联 coordinator 与设备信息。"""
        self.coordinator = coordinator
        self._device_name = device_name
        self._entry_id = entry_id
        self._device_id = device_id
        self._phonenum_last4 = phonenum_last4
        self._attr_suggested_object_id = format_entity_id("ct", phonenum_last4, "balance")
        self._attr_has_entity_name = True

    @property
    def name(self):
        return ENTITY_BALANCE

    @property
    def unique_id(self):
        return f"CT_{self._phonenum_last4}_balance"

    @property
    def state(self):
        """主状态 = 当前余额 - 欠费，欠费时显示负值。"""
        data = self.coordinator.data
        if not data:
            return None
        try:
            balance = float(data.get("balance") or 0)
            arrear = float(data.get("arrear") or 0)
            return round(balance - arrear, 2)
        except (TypeError, ValueError):
            return data.get("balance")

    @property
    def unit_of_measurement(self):
        return "元"

    @property
    def icon(self):
        return "mdi:cash"

    @property
    def extra_state_attributes(self):
        """包含本设备所有实体的数据。"""
        data = self.coordinator.data or {}
        return {
            "账户余额": data.get("balance"),
            "本月消费": data.get("currentMonthCost"),
            "欠费": data.get("arrear"),
            "流量总量": data.get("flowTotal"),
            "流量已用": data.get("flowUse"),
            "流量剩余": data.get("flowBalance"),
            "流量超出": data.get("flowOver"),
            "流量使用率": data.get("percentUsed"),
            "通用流量总量": data.get("commonTotal"),
            "通用流量已用": data.get("commonUse"),
            "通用流量剩余": data.get("commonBalance"),
            "专用流量总量": data.get("specialTotal"),
            "专用流量已用": data.get("specialUse"),
            "专用流量剩余": data.get("specialBalance"),
            "通话总量": data.get("voiceTotal"),
            "通话已用": data.get("voiceUsage"),
            "通话剩余": data.get("voiceBalance"),
            "通话使用率": data.get("voicePercentUsed"),
            "短信总量": data.get("smsTotal"),
            "短信已用": data.get("smsUsage"),
            "短信剩余": data.get("smsBalance"),
            "短信使用率": data.get("smsPercentUsed"),
            "最近刷新时间": data.get("lastRefreshTime"),
        }

    @property
    def should_poll(self):
        return False

    @property
    def available(self):
        return self.coordinator.last_update_success

    @property
    def device_info(self):
        return device_info(self._entry_id, self._device_name, OPERATOR_TELECOM)

    async def async_added_to_hass(self):
        ensure_short_entity_id(
            self.hass, self.entity_id,
            format_entity_id("ct", self._phonenum_last4, "balance"),
        )
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )


class ChinaTelecomDataUpdateCoordinator(DataUpdateCoordinator):
    """
    中国电信数据更新协调器。

    继承 Home Assistant 的 DataUpdateCoordinator，负责：
    - 每 N 分钟执行一次数据更新（登录 -> 查询套餐 -> 解析）
    - 将解析后的数据提供给所有 ChinaTelecomSensor 实体
    - 若 API 返回 X201（token 过期），自动重新登录后重试查询
    - 支持登录冷却机制，避免频繁登录导致账户被锁定
    - 支持缓存登录信息，减少登录次数
    """

    def __init__(self, hass, entry, phonenum, password, refresh_interval=15):
        """初始化协调器，设置更新间隔。"""
        self.phonenum = phonenum
        self.password = password
        self.entry = entry
        self.telecom_device_id = (
            entry.options.get(CONF_TELECOM_DEVICE_ID)
            or entry.data.get(CONF_TELECOM_DEVICE_ID)
            or DEFAULT_TELECOM_DEVICE_ID
        ).strip()
        self.update_interval_minutes = refresh_interval
        self._login_attempts = hass.data.setdefault(DOMAIN, {}).setdefault("login_attempts", {})
        self.telecom = Telecom()
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=self.update_interval_minutes)
        )

    @property
    def masked_phonenum(self):
        """返回脱敏后的手机号。"""
        return f"{self.phonenum[:3]}****{self.phonenum[7:]}"

    def _response_data(self, payload):
        """从响应中提取 responseData。"""
        if not isinstance(payload, dict):
            return {}
        response_data = payload.get("responseData")
        return response_data if isinstance(response_data, dict) else {}

    def _response_inner_data(self, payload):
        """从响应中提取 responseData.data。"""
        data = self._response_data(payload).get("data")
        return data if isinstance(data, dict) else {}

    def _extract_error_msg(self, payload, default):
        """从响应中提取错误信息。"""
        if not isinstance(payload, dict):
            return default

        response_data = self._response_data(payload)
        data = self._response_inner_data(payload)
        login_fail = data.get("loginFailResult") or {}
        header_infos = payload.get("headerInfos") or {}

        result_code = response_data.get("resultCode")
        if result_code == "3006":
            return "3006: 设备未信任，请通过上游短信授权获取 DeviceId，并在集成选项中填写"
        if result_code == "3005":
            return "3005: 服务端要求验证码/二次校验"

        result_desc = (
            login_fail.get("reason")
            or response_data.get("resultDesc")
            or response_data.get("resultMsg")
            or response_data.get("msg")
            or payload.get("error")
            or header_infos.get("reason")
            or header_infos.get("resultDesc")
            or header_infos.get("msg")
        )
        if result_code and result_desc:
            return f"{result_code}: {result_desc}"
        return result_desc or result_code or default

    def _is_token_expired(self, payload):
        """判断 token 是否过期（X201）。"""
        if not isinstance(payload, dict):
            return False

        header_infos = payload.get("headerInfos") or {}
        response_data = self._response_data(payload)
        return header_infos.get("code") == "X201" or response_data.get("resultCode") == "X201"

    def _login_cooldown_remaining_seconds(self):
        """计算登录冷却剩余时间（秒）。"""
        last_attempt = self._login_attempts.get(self.entry.entry_id)
        if last_attempt is None:
            return 0

        cooldown_seconds = LOGIN_RETRY_COOLDOWN_MINUTES * 60
        elapsed_seconds = time.monotonic() - last_attempt
        return max(0, int(cooldown_seconds - elapsed_seconds))

    def _log_payload(self, message, payload):
        """安全地记录日志（脱敏处理）。"""
        _LOGGER.error(
            "%s for %s: %s",
            message,
            self.masked_phonenum,
            self.telecom.format_for_log(payload),
        )

    async def _process_important_data(self, important_data_raw):
        """处理重要数据响应，转换为传感器格式。"""
        important_data = self._response_inner_data(important_data_raw)

        flow_info = important_data.get("flowInfo")
        voice_info = important_data.get("voiceInfo")
        has_flow_data = isinstance(flow_info, dict) and any(
            isinstance(flow_info.get(key), dict)
            for key in ("totalAmount", "commonFlow", "specialAmount")
        )
        has_voice_data = isinstance(voice_info, dict) and isinstance(
            voice_info.get("voiceDataInfo"), dict
        )
        has_package_data = has_flow_data or has_voice_data

        if not has_package_data and self.data:
            _LOGGER.warning(
                "China Telecom returned balance-only data for %s; keeping the previous sensor values.",
                self.masked_phonenum,
            )
            return dict(self.data)

        summary_data = await self.hass.async_add_executor_job(
            self.telecom.to_summary, important_data, self.phonenum
        )

        processed_data = {
            "balance": round(summary_data.get("balance", 0) / 100, 2),
            "currentMonthCost": round(summary_data.get("currentMonthCost", 0) / 100, 2),
            "arrear": round(summary_data.get("arrear", 0) / 100, 2),
            "voiceUsage": summary_data.get("voiceUsage", 0),
            "voiceBalance": summary_data.get("voiceBalance", 0),
            "voiceTotal": summary_data.get("voiceTotal", 0),
            "smsUsage": summary_data.get("smsUsage", 0),
            "smsBalance": summary_data.get("smsBalance", 0),
            "smsTotal": summary_data.get("smsTotal", 0),
            "flowUse": round(self.telecom.convert_flow(summary_data.get("flowUse", 0), "GB", 2), 2),
            "flowTotal": round(self.telecom.convert_flow(summary_data.get("flowTotal", 0), "GB", 2), 2),
            "flowBalance": round(self.telecom.convert_flow(summary_data.get("flowTotal", 0) - summary_data.get("flowUse", 0), "GB", 2), 2),
            "flowOver": round(self.telecom.convert_flow(summary_data.get("flowOver", 0), "GB", 2), 2),
            "commonUse": round(self.telecom.convert_flow(summary_data.get("commonUse", 0), "GB", 2), 2),
            "commonTotal": round(self.telecom.convert_flow(summary_data.get("commonTotal", 0), "GB", 2), 2),
            "commonBalance": round(self.telecom.convert_flow(summary_data.get("commonTotal", 0) - summary_data.get("commonUse", 0), "GB", 2), 2),
            "commonOver": round(self.telecom.convert_flow(summary_data.get("commonOver", 0), "GB", 2), 2),
            "specialUse": round(self.telecom.convert_flow(summary_data.get("specialUse", 0), "GB", 2), 2),
            "specialTotal": round(self.telecom.convert_flow(summary_data.get("specialTotal", 0), "GB", 2), 2),
            "specialBalance": round(self.telecom.convert_flow(summary_data.get("specialTotal", 0) - summary_data.get("specialUse", 0), "GB", 2), 2),
            "points": summary_data.get("points", 0),
            "lastRefreshTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if has_package_data
            else None,
        }

        # 计算流量使用率
        if processed_data["flowTotal"] > 0:
            processed_data["percentUsed"] = round((processed_data["flowUse"] / processed_data["flowTotal"]) * 100, 2)
        else:
            processed_data["percentUsed"] = 0

        # 计算通话使用率
        if processed_data["voiceTotal"] > 0:
            processed_data["voicePercentUsed"] = round((processed_data["voiceUsage"] / processed_data["voiceTotal"]) * 100, 1)
        else:
            processed_data["voicePercentUsed"] = 0

        # 计算短信使用率
        if processed_data["smsTotal"] > 0:
            processed_data["smsPercentUsed"] = round((processed_data["smsUsage"] / processed_data["smsTotal"]) * 100, 2)
        else:
            processed_data["smsPercentUsed"] = 0

        _LOGGER.debug(f"Processed data before returning: {processed_data}")
        return processed_data

    async def _try_cached_login_info(self):
        """尝试使用缓存的登录信息。"""
        cached_login_info = self.entry.data.get("login_info")
        if not isinstance(cached_login_info, dict):
            return None
        if not cached_login_info.get("token"):
            _LOGGER.debug("Cached China Telecom login info has no token for %s.", self.masked_phonenum)
            return None

        cached_login_info = {**cached_login_info}
        cached_login_info["phonenum"] = self.phonenum
        cached_login_info["password"] = self.password
        self.telecom.set_login_info(cached_login_info)
        _LOGGER.debug("Trying cached China Telecom token for %s.", self.masked_phonenum)

        important_data_raw = await self.hass.async_add_executor_job(
            self.telecom.qry_important_data
        )
        if self._response_inner_data(important_data_raw):
            _LOGGER.debug("Successfully fetched China Telecom data with cached token for %s.", self.masked_phonenum)
            return await self._process_important_data(important_data_raw)

        error_msg = self._extract_error_msg(important_data_raw, "缓存 token 不可用")
        if self._is_token_expired(important_data_raw):
            _LOGGER.warning("Cached China Telecom token expired for %s: %s", self.masked_phonenum, error_msg)
            return None

        _LOGGER.error("Cached China Telecom token query failed for %s: %s", self.masked_phonenum, error_msg)
        self._log_payload("China Telecom qryImportantData response with cached token", important_data_raw)
        raise UpdateFailed(f"Cached token query failed: {error_msg}")

    def _store_login_info(self, login_info):
        """存储登录信息到配置项。"""
        cacheable_login_info = {
            key: value
            for key, value in login_info.items()
            if key not in {"password", "phonenum"}
        }
        new_data = {**self.entry.data, "login_info": cacheable_login_info}
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _login_and_store(self, reason):
        """执行登录并存储登录信息。"""
        remaining_seconds = self._login_cooldown_remaining_seconds()
        if remaining_seconds > 0:
            remaining_minutes = max(1, (remaining_seconds + 59) // 60)
            raise UpdateFailed(
                f"Login skipped: 登录冷却中，请约 {remaining_minutes} 分钟后重试。原因: {reason}"
            )

        self._login_attempts[self.entry.entry_id] = time.monotonic()
        _LOGGER.warning(
            "China Telecom token unavailable for %s (%s). Performing one login; next login is cooled down for %s minutes.",
            self.masked_phonenum,
            reason,
            LOGIN_RETRY_COOLDOWN_MINUTES,
        )
        login_result = await self.hass.async_add_executor_job(
            self.telecom.do_login,
            self.phonenum,
            self.password,
            self.telecom_device_id,
        )

        login_response_data = self._response_data(login_result)
        if login_response_data.get("resultCode") != "0000":
            error_msg = self._extract_error_msg(login_result, "未知登录失败")
            _LOGGER.error(f"Login failed for {self.masked_phonenum}: {error_msg}")
            self._log_payload("China Telecom login response", login_result)
            raise UpdateFailed(f"Login failed: {error_msg}")

        login_info = self._response_inner_data(login_result).get("loginSuccessResult")
        if not isinstance(login_info, dict):
            self._log_payload("China Telecom login response", login_result)
            raise UpdateFailed("Login failed: 登录成功响应缺少 loginSuccessResult")

        login_info["phonenum"] = self.phonenum
        login_info["password"] = self.password
        self.telecom.set_login_info(login_info)
        self._store_login_info(login_info)
        _LOGGER.debug(f"Successfully logged in for {self.masked_phonenum}.")
        return login_result

    async def _async_update_data(self):
        """
        执行数据更新逻辑，由 DataUpdateCoordinator 定期调用。

        流程：
        1. 尝试使用缓存的 token 查询数据
        2. 缓存无效时执行登录并存储登录信息
        3. 使用新 token 查询数据
        4. 解析数据并返回结构化结果

        若 token 过期（X201），自动重新登录后再次查询。
        """
        login_result = None
        important_data_raw = None
        try:
            # 尝试使用缓存的登录信息
            cached_data = await self._try_cached_login_info()
            if cached_data is not None:
                return cached_data

            # 执行登录
            login_result = await self._login_and_store("没有可用缓存 token 或缓存 token 已过期")

            # 获取重要数据
            important_data_raw = await self.hass.async_add_executor_job(
                self.telecom.qry_important_data
            )

            _LOGGER.info(f"qryImportantData raw response: {important_data_raw}")
            
            important_data = self._response_inner_data(important_data_raw)
            _LOGGER.info(f"Parsed important_data: {important_data}")
            
            if important_data:
                _LOGGER.debug(f"Successfully fetched important data for {self.masked_phonenum}.")
                return await self._process_important_data(important_data_raw)

            error_msg = self._extract_error_msg(important_data_raw, "未知数据获取失败")
            if self._is_token_expired(important_data_raw):
                _LOGGER.error(
                    "Fresh China Telecom token was rejected for %s: %s. Skip immediate re-login to avoid repeated login risk.",
                    self.masked_phonenum,
                    error_msg,
                )
            else:
                _LOGGER.error(f"Failed to fetch data for {self.masked_phonenum}: {error_msg}")
            self._log_payload("China Telecom qryImportantData response after login", important_data_raw)
            raise UpdateFailed(f"Failed to fetch data after login: {error_msg}")

        except UpdateFailed:
            raise
        except Exception as error:
            if login_result is not None:
                self._log_payload("China Telecom last login response before exception", login_result)
            if important_data_raw is not None:
                self._log_payload("China Telecom last qryImportantData response before exception", important_data_raw)
            _LOGGER.error(f"Error fetching China Telecom data: {error}", exc_info=True)
            raise UpdateFailed(f"Error fetching China Telecom data: {error}")


class ChinaTelecomSensor(BaseOperatorSingleSensor):
    """中国电信单项传感器，从 coordinator.data[key] 读取状态。"""

    def __init__(self, coordinator, device_name, key, name, unit, icon, entry_id, device_id, phonenum_last4, entity_suffix=None):
        super().__init__(
            coordinator, device_name, key, name, unit, icon,
            entry_id, device_id, phonenum_last4,
            "CT", OPERATOR_TELECOM,
            entity_suffix,
        )