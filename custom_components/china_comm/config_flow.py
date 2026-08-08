"""
移动运营商集成 - 配置流程模块

本模块实现 Home Assistant 的配置流程（Config Flow），负责：

1. 第一步：用户选择运营商类型（电信/联通）
2. 第二步：根据类型展示对应配置表单：
   - 电信：手机号 + 服务密码（提交时调用 API 验证登录）
   - 联通：名称 + OpenID + 刷新间隔
3. 选项流程：联通/电信支持通过「配置」按钮修改参数
"""

import re
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_TELECOM,
    ENTRY_TYPE_UNICOM,
    CONF_PHONENUM,
    CONF_PASSWORD,
    CONF_OPENID,
    CONF_REFRESH_INTERVAL,
    CONF_TELECOM_DEVICE_ID,
    DEFAULT_TELECOM_DEVICE_ID,
    OPERATOR_TELECOM,
    OPERATOR_UNICOM,
    format_entry_title,
)

_LOGGER = logging.getLogger(__name__)

_TELECOM_RESULT_ERROR_KEYS = {
    "3005": "login_need_captcha",
    "3006": "login_device_untrusted",
    "3007": "login_too_frequent",
    "3008": "login_account_locked",
    "3009": "login_password_locked",
}


def validate_phone_number(phone):
    """
    验证手机号码格式，必须为 11 位数字。

    Raises:
        vol.Invalid: 格式不符合 11 位数字时抛出
    """
    if not phone or not re.match(r"^\d{11}$", phone):
        raise vol.Invalid("invalid_phone_number")
    return phone


class ChinaCommConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """移动运营商配置流程。"""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        """配置流程第一步：选择运营商类型。"""
        if user_input is not None:
            entry_type = user_input[CONF_ENTRY_TYPE]
            if entry_type == ENTRY_TYPE_TELECOM:
                return await self.async_step_telecom()
            return await self.async_step_unicom_phone()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTRY_TYPE, default=ENTRY_TYPE_UNICOM): vol.In(
                        {
                            ENTRY_TYPE_UNICOM: "中国联通",
                            ENTRY_TYPE_TELECOM: "中国电信",
                        }
                    ),
                }
            ),
            description_placeholders={"name": "移动运营商"},
        )

    async def async_step_telecom(self, user_input=None):
        """中国电信配置步骤：手机号、服务密码。"""
        errors = {}
        placeholders = {"name": "中国电信", "error": ""}

        if user_input is not None:
            try:
                user_input[CONF_PHONENUM] = validate_phone_number(
                    user_input[CONF_PHONENUM]
                )
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_TELECOM

                from .CT.api import Telecom

                telecom = Telecom()
                login_result = await self.hass.async_add_executor_job(
                    telecom.do_login,
                    user_input[CONF_PHONENUM],
                    user_input[CONF_PASSWORD],
                    user_input.get(CONF_TELECOM_DEVICE_ID, ""),
                )

                if login_result.get("error"):
                    errors["base"] = "login_connection_failed"
                    placeholders["error"] = str(login_result["error"])
                    _LOGGER.error("电信登录请求失败: %s", login_result["error"])
                elif not login_result.get("responseData"):
                    errors["base"] = "login_server_error"
                    _LOGGER.warning("电信登录返回异常: %s", login_result)
                elif login_result.get("responseData", {}).get("resultCode") != "0000":
                    resp_data = login_result.get("responseData", {})
                    data_obj = resp_data.get("data", {})
                    fail_result = data_obj.get("loginFailResult", {})
                    result_code = resp_data.get("resultCode", "")

                    if result_code in _TELECOM_RESULT_ERROR_KEYS:
                        errors["base"] = _TELECOM_RESULT_ERROR_KEYS[result_code]
                    else:
                        error_msg = (
                            fail_result.get("reason")
                            or resp_data.get("resultDesc")
                            or ""
                        )
                        if error_msg:
                            errors["base"] = "login_failed"
                            placeholders["error"] = str(error_msg)
                        else:
                            errors["base"] = "login_invalid_credentials"
                    _LOGGER.warning(
                        "电信登录验证失败 [错误码: %s]", result_code
                    )
                else:
                    unique_id = f"telecom_{user_input[CONF_PHONENUM]}"
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=format_entry_title(
                            OPERATOR_TELECOM, user_input[CONF_PHONENUM]
                        ),
                        data=user_input,
                    )
            except vol.Invalid:
                errors["base"] = "invalid_phone_number"
                _LOGGER.error("电信配置验证失败: 无效手机号")
            except Exception:
                errors["base"] = "unknown_error_config"
                _LOGGER.exception("电信配置异常")

        return self.async_show_form(
            step_id="telecom",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONENUM): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_TELECOM_DEVICE_ID, default=DEFAULT_TELECOM_DEVICE_ID
                    ): str,
                    vol.Required(CONF_REFRESH_INTERVAL, default=15): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=60)
                    ),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_unicom_phone(self, user_input=None):
        """中国联通第一步：可选输入手机号。"""
        if user_input is not None:
            self.context["unicom_phonenum"] = user_input.get(CONF_PHONENUM, "").strip()
            return await self.async_step_unicom()

        return self.async_show_form(
            step_id="unicom_phone",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PHONENUM, default=""): str,
                }
            ),
            description_placeholders={"name": "中国联通"},
        )

    async def async_step_unicom(self, user_input=None):
        """中国联通配置步骤：名称、OpenID、刷新间隔。"""
        errors = {}
        phonenum = self.context.get("unicom_phonenum", "")

        if user_input is not None:
            if not user_input.get(CONF_OPENID):
                errors["base"] = "openid_required"
            else:
                user_input[CONF_PHONENUM] = user_input.get(CONF_PHONENUM) or phonenum
                user_input[CONF_ENTRY_TYPE] = ENTRY_TYPE_UNICOM

                unique_id = f"unicom_{user_input[CONF_OPENID]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=format_entry_title(
                        OPERATOR_UNICOM,
                        user_input.get(CONF_PHONENUM)
                        or user_input.get("name", "中国联通"),
                    ),
                    data=user_input,
                )

        return self.async_show_form(
            step_id="unicom",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="中国联通"): str,
                    vol.Required(CONF_OPENID): str,
                    vol.Optional(CONF_PHONENUM, default=phonenum): str,
                    vol.Required(CONF_REFRESH_INTERVAL, default=15): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=60)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"name": "中国联通"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """返回选项流程处理器。"""
        return ChinaCommOptionsFlow()


class ChinaCommOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """移动运营商选项流程。"""

    def _safe_refresh_interval(self, entry_data):
        """安全获取刷新间隔，确保为 1-60 的整数。"""
        val = entry_data.get(CONF_REFRESH_INTERVAL, 15)
        try:
            val = int(val) if val is not None else 15
        except (TypeError, ValueError):
            val = 15
        return max(1, min(60, val))

    async def async_step_init(self, user_input=None):
        """处理选项流程的初始化步骤。"""
        try:
            entry_data = self.config_entry.data or {}

            if entry_data.get(CONF_ENTRY_TYPE) not in (
                ENTRY_TYPE_UNICOM,
                ENTRY_TYPE_TELECOM,
            ):
                return self.async_abort(reason="no_options")

            if entry_data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TELECOM:
                if user_input is not None:
                    new_data = {**entry_data, **user_input}
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data
                    )
                    return self.async_create_entry(title="", data={})

                telecom_device_id = (
                    self.config_entry.options.get(CONF_TELECOM_DEVICE_ID)
                    or entry_data.get(CONF_TELECOM_DEVICE_ID)
                    or ""
                )

                return self.async_show_form(
                    step_id="init",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_PASSWORD,
                                default=entry_data.get(CONF_PASSWORD, ""),
                            ): str,
                            vol.Optional(
                                CONF_TELECOM_DEVICE_ID,
                                default=telecom_device_id,
                            ): str,
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                default=entry_data.get(CONF_REFRESH_INTERVAL, 15),
                            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                        }
                    ),
                )

            if user_input is not None:
                new_data = {**entry_data, **user_input}
                new_title = format_entry_title(
                    OPERATOR_UNICOM,
                    new_data.get(CONF_PHONENUM) or new_data.get("name", "中国联通"),
                )
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data, title=new_title
                )
                return self.async_create_entry(title="", data={})

            name_default = entry_data.get("name") or "中国联通"
            openid_default = entry_data.get(CONF_OPENID) or ""
            phonenum_default = entry_data.get(CONF_PHONENUM) or ""
            refresh_default = self._safe_refresh_interval(entry_data)

            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Required("name", default=str(name_default)): str,
                        vol.Required(CONF_OPENID, default=str(openid_default)): str,
                        vol.Optional(
                            CONF_PHONENUM, default=str(phonenum_default)
                        ): str,
                        vol.Required(
                            CONF_REFRESH_INTERVAL,
                            default=refresh_default,
                        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                    }
                ),
            )
        except Exception as e:
            _LOGGER.exception("选项流程加载失败: %s", e)
            return self.async_abort(reason="unknown_error_config")
