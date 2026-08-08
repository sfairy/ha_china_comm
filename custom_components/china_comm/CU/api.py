"""
中国联通 - API 封装模块

封装中国联通小程序（mina.10010.com）的 HTTP 接口，包括：
- getTicket：获取鉴权 ticket
- serviceEntrance：获取 microHall Cookie
- sspbigball：获取概览数据
- accountBalancenew：获取账户余额详情
- queryDetail：获取本月账单详情
- queryOcsPackageFlowLeftContent：获取语音、短信、流量详细用量

鉴权方式：通过 OpenID（从联通小程序获取）进行身份验证，无需密码。
"""

import json
import logging
import time
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# 联通小程序 API 端点
API_GET_TICKET = "https://mina.10010.com/wxapplet/weixinNew/getTicket"
API_SERVICE_ENTRANCE = (
    "https://mxx.client.10010.com/servicebusiness/wx/serviceEntrance"
)
API_SSPBIGBALL = "https://mina.10010.com/wxapplet/weixinNew/sspbigball"
API_USAGE_DETAIL = (
    "https://mxx.client.10010.com/servicequerybusiness"
    "/operationservice/queryOcsPackageFlowLeftContentRevisedInJune"
)
API_BALANCE_DETAIL = (
    "https://mxx.client.10010.com/servicequerybusiness"
    "/balancenew/accountBalancenew.htm"
)
API_BILL_DETAIL = (
    "https://m.client.10010.com/serviceimportantbusiness"
    "/phoneBillNew/queryDetail"
)
API_QUERY_GOODS_LIST = "https://mina.10010.com/wxapplet/weixinNew/queryGoodsList"

# 请求头
HEADERS_JSON = {"Content-Type": "application/json"}
HEADERS_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _safe_float(value: str | float | None) -> float:
    """安全转换为 float，失败返回 0.0。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _fmt_num(value: float) -> str:
    """数值转字符串，整数不保留小数。"""
    return str(int(value)) if value == int(value) else str(value)


def _extract_data_item(item: dict) -> dict[str, Any]:
    """从 API 流量明细项提取统一字段。"""
    return {
        "addUpItemName": item.get("addUpItemName"),
        "use": item.get("use"),
        "total": item.get("total"),
        "remain": item.get("remain"),
        "xexceedvalue": item.get("xexceedvalue"),
        "usedPercent": item.get("usedPercent"),
        "endDate": item.get("endDate"),
        "beforeTotal": item.get("beforeTotal"),
        "beforeRemain": item.get("beforeRemain"),
        "beforeUse": item.get("beforeUse"),
        "flowType": item.get("flowType"),
        "feePolicyName": item.get("feePolicyName"),
    }


class ChinaUnicom:
    """中国联通小程序 API 客户端。

    负责鉴权（ticket + Cookie）、数据拉取（流量/语音/短信/余额/账单）。
    鉴权流程：openId -> getTicket -> serviceEntrance(Cookie) -> 各业务API

    支持手动传入已获取的 ticket/Cookie 跳过鉴权步骤。
    """

    def __init__(
        self,
        openid: str,
        usage_ticket: str = "",
        micro_hall_user: str = "",
        micro_hall_access_token: str = "",
    ):
        """初始化 API 客户端。

        Args:
            openid: 联通小程序 OpenID
            usage_ticket: 手动传入的 usage ticket（可选）
            micro_hall_user: 手动传入的 microHallUser Cookie（可选）
            micro_hall_access_token: 手动传入的 microHallAccessToken Cookie（可选）
        """
        self.openid = openid

        # 手动凭证
        self._manual_usage_ticket = usage_ticket
        self._manual_micro_hall_user = micro_hall_user
        self._manual_micro_hall_access_token = micro_hall_access_token

        # 自动获取的凭证
        self._auto_ticket = ""
        self._auto_ticket_phone = ""
        self._micro_hall_user = micro_hall_user
        self._micro_hall_access_token = micro_hall_access_token

    def _build_cookie_header(self) -> str:
        """构建 Cookie 头，用于 mxx.client.10010.com 的请求鉴权。"""
        cookies = []
        if self._micro_hall_user:
            cookies.append(f"microHallUser={self._micro_hall_user}")
        if self._micro_hall_access_token:
            cookies.append(f"microHallAccessToken={self._micro_hall_access_token}")
        return "; ".join(cookies)

    async def _auto_get_auth(self, session: aiohttp.ClientSession) -> None:
        """自动获取鉴权凭证（ticket + Cookie）。

        流程：
        1. getTicket：获取鉴权 ticket
        2. serviceEntrance：通过 ticket 跳转获取 microHall Cookie
        """
        try:
            # Step 1: 获取 ticket
            ticket_payload = {"openId": self.openid, "channel": "wxmini"}
            async with session.post(
                API_GET_TICKET, json=ticket_payload, headers=HEADERS_JSON
            ) as resp:
                text = await resp.text()
                try:
                    ticket_json = json.loads(text)
                except json.JSONDecodeError:
                    _LOGGER.debug("getTicket 返回非 JSON: %s", text[:200])
                    return

                if ticket_json.get("code") != "0000":
                    _LOGGER.debug("getTicket 失败: code=%s", ticket_json.get("code"))
                    return

                ticket = ticket_json.get("data", "")
                self._auto_ticket = ticket
                self._auto_ticket_phone = f"wx{int(time.time() * 1000)}"
                _LOGGER.debug(
                    "getTicket 成功: %s...",
                    ticket[:20] if len(str(ticket)) > 20 else ticket,
                )

            # Step 2: 通过 serviceEntrance 获取 Cookie
            try:
                entrance_url = (
                    f"{API_SERVICE_ENTRANCE}"
                    f"?ticket={ticket}"
                    f"&servicecode=YH10007"
                    f"&ticketChannel=XCXSYHF"
                )
                async with session.get(entrance_url) as entrance_resp:
                    for cookie_str in entrance_resp.headers.getall("Set-Cookie", []):
                        if "microHallUser=" in cookie_str:
                            self._micro_hall_user = (
                                cookie_str.split("microHallUser=", 1)[1]
                                .split(";")[0]
                                .strip()
                            )
                        if "microHallAccessToken=" in cookie_str:
                            self._micro_hall_access_token = (
                                cookie_str.split("microHallAccessToken=", 1)[1]
                                .split(";")[0]
                                .strip()
                            )

                    if self._micro_hall_user:
                        _LOGGER.debug("serviceEntrance 成功，获取 microHall Cookie")
                    else:
                        _LOGGER.debug("serviceEntrance 未返回 Cookie")

            except Exception as err:
                _LOGGER.debug("serviceEntrance 失败 (%s): %s", type(err).__name__, err)

        except Exception as err:
            _LOGGER.debug("自动鉴权失败 (%s): %s", type(err).__name__, err)

    async def get_overview(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """从 sspbigball 获取概览数据。"""
        try:
            payload = {"openid": self.openid, "channel": "wxmini"}
            async with session.post(
                API_SSPBIGBALL, json=payload, headers=HEADERS_JSON
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                if data.get("code") == "0000":
                    return data.get("data", {})
                else:
                    _LOGGER.debug("sspbigball 返回错误: code=%s", data.get("code"))
                    return {}

        except Exception as err:
            _LOGGER.error("获取概览数据失败: %s", err)
            return {}

    async def get_phone_number(self, session: aiohttp.ClientSession) -> str | None:
        """从 API 中自动检测完整手机号。

        优先尝试 queryGoodsList API（返回完整号码），
        失败时回退到 usage detail API。
        """
        # 方法1：queryGoodsList API
        try:
            payload = {"openid": self.openid, "channel": "wxmini"}
            async with session.post(
                API_QUERY_GOODS_LIST, json=payload, headers=HEADERS_JSON
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                if data.get("code") == "0000" and data.get("data", {}).get("res"):
                    res_list = data["data"]["res"]
                    for item in res_list:
                        main_number = item.get("mainNumber", "")
                        if (
                            main_number
                            and len(main_number) == 11
                            and main_number.startswith("1")
                        ):
                            _LOGGER.info(
                                "从 queryGoodsList 获取完整手机号: %s", main_number
                            )
                            return main_number
        except Exception as err:
            _LOGGER.debug("queryGoodsList API 失败: %s", err)

        # 方法2：usage detail API 兜底
        try:
            effective_ticket = self._manual_usage_ticket or self._auto_ticket
            effective_phone = self._auto_ticket_phone

            form_data = {
                "duanlianjieabc": "",
                "channelCode": "",
                "serviceType": "",
                "saleChannel": "",
                "externalSources": "",
                "contactCode": "",
                "ticket": effective_ticket,
                "ticketPhone": effective_phone,
                "ticketChannel": "XCXYLCXYY",
                "language": "chinese",
            }

            headers = HEADERS_FORM.copy()
            cookie_header = self._build_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header

            async with session.post(
                API_USAGE_DETAIL, data=form_data, headers=headers
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                if data.get("shareData") and data["shareData"].get("details"):
                    for item in data["shareData"]["details"]:
                        vice_list = item.get("viceCardlist", [])
                        if vice_list and len(vice_list) > 0:
                            user_number = vice_list[0].get("usernumber", "")
                            if user_number and len(user_number) >= 7:
                                _LOGGER.info(
                                    "从 usage detail API 获取手机号: %s", user_number
                                )
                                return user_number

                return None

        except Exception as err:
            _LOGGER.error("从 usage detail API 获取手机号失败: %s", err)
            return None

    async def get_balance_detail(
        self, session: aiohttp.ClientSession
    ) -> dict[str, Any]:
        """从 accountBalancenew API 获取账户余额详情。

        返回字段包括：
        - canusefeecustNew / canusefeecust: 可用余额
        - curntbalancecust: 当前余额（上月结余）
        - totalrealfee / realfeecustnew: 本月话费
        - allbowefeecust: 账户欠费
        - canuselimitcust: 信用额度
        """
        try:
            effective_ticket = self._manual_usage_ticket or self._auto_ticket
            effective_phone = self._auto_ticket_phone

            form_data = {
                "duanlianjieabc": "",
                "channelCode": "",
                "serviceType": "",
                "saleChannel": "",
                "externalSources": "",
                "contactCode": "",
                "ticket": effective_ticket,
                "ticketPhone": effective_phone,
                "ticketChannel": "XCXSYHF",
                "language": "chinese",
                "channel": "client",
            }

            headers = HEADERS_FORM.copy()
            cookie_header = self._build_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header

            async with session.post(
                API_BALANCE_DETAIL, data=form_data, headers=headers
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                if data.get("code") == "0000":
                    _LOGGER.debug("余额详情获取成功")
                    return data.get("data", data)
                else:
                    _LOGGER.debug(
                        "余额详情错误 code=%s: %s",
                        data.get("code"),
                        data.get("msg", ""),
                    )
                    return {}

        except Exception as err:
            _LOGGER.error("获取余额详情失败: %s", err)
            return {}

    async def get_bill_detail(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """从 queryDetail API 获取本月账单详情。"""
        try:
            effective_ticket = self._manual_usage_ticket or self._auto_ticket
            effective_phone = self._auto_ticket_phone
            current_month = time.strftime("%Y%m")

            form_data = {
                "duanlianjieabc": "",
                "channelCode": "",
                "serviceType": "",
                "saleChannel": "",
                "externalSources": "",
                "contactCode": "",
                "ticket": effective_ticket,
                "ticketPhone": effective_phone,
                "ticketChannel": "XCXSYHF",
                "month": current_month,
            }

            headers = HEADERS_FORM.copy()
            cookie_header = self._build_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header

            async with session.post(
                API_BILL_DETAIL, data=form_data, headers=headers
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                if data.get("code") == "0000":
                    _LOGGER.debug("账单详情获取成功")
                    return data.get("data", {})
                else:
                    _LOGGER.debug(
                        "账单详情错误 code=%s: %s",
                        data.get("code"),
                        data.get("desc", ""),
                    )
                    return {}

        except Exception as err:
            _LOGGER.error("获取账单详情失败: %s", err)
            return {}

    def _parse_usage_response(self, data: dict) -> dict[str, Any]:
        """解析用量 API 响应。"""
        parsed: dict[str, Any] = {"data_items": []}

        def _append_data_from_details(details: list) -> None:
            for item in details:
                if isinstance(item, dict) and item.get("elemType") == "3":
                    parsed["data_items"].append(_extract_data_item(item))

        # 优先：unshared（新版主要来源）
        if data.get("unshared") and isinstance(data["unshared"], list):
            for group in data["unshared"]:
                if not isinstance(group, dict):
                    continue
                details = group.get("details", [])
                if isinstance(details, list):
                    _append_data_from_details(details)

        # 回退：shareData
        if not parsed["data_items"] and data.get("shareData") and data["shareData"].get("details"):
            details = data["shareData"].get("details", [])
            if isinstance(details, list):
                _append_data_from_details(details)

        # 回退：resources 中的流量项
        if not parsed["data_items"] and data.get("resources"):
            resources = data.get("resources", [])
            if isinstance(resources, list):
                for group in resources:
                    if not isinstance(group, dict):
                        continue
                    details = group.get("details", [])
                    if isinstance(details, list):
                        _append_data_from_details(details)

        # 解析语音和短信：先累加 details，再回退到资源组级汇总
        voice_items: list[dict] = []
        sms_items: list[dict] = []
        voice_group_remain = None
        voice_group_use = None
        sms_group_remain = None
        sms_group_use = None

        resources = data.get("resources", [])
        if isinstance(resources, list):
            for group in resources:
                if not isinstance(group, dict):
                    continue

                gtype = group.get("type", "")
                if gtype == "Voice":
                    try:
                        voice_group_use = float(group.get("userResource", 0))
                        voice_group_remain = float(group.get("remainResource", 0))
                    except (ValueError, TypeError):
                        pass
                elif gtype == "smsList":
                    try:
                        sms_group_use = float(group.get("userResource", 0))
                        sms_group_remain = float(group.get("remainResource", 0))
                    except (ValueError, TypeError):
                        pass

                details = group.get("details", [])
                if not isinstance(details, list):
                    continue
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    elem_type = item.get("elemType")
                    if elem_type == "1":
                        voice_items.append(item)
                    elif elem_type == "2":
                        sms_items.append(item)

        if voice_items:
            total_use = sum(_safe_float(i.get("use")) for i in voice_items)
            total_total = sum(_safe_float(i.get("total")) for i in voice_items)
            total_remain = sum(_safe_float(i.get("remain")) for i in voice_items)
            used_pct = round(total_use / total_total * 100) if total_total > 0 else 0
            parsed["voice"] = {
                "use": _fmt_num(total_use),
                "total": _fmt_num(total_total),
                "remain": _fmt_num(total_remain),
                "usedPercent": str(used_pct),
            }
        elif voice_group_use is not None and voice_group_remain is not None:
            total = voice_group_use + voice_group_remain
            parsed["voice"] = {
                "use": _fmt_num(voice_group_use),
                "total": _fmt_num(total),
                "remain": _fmt_num(voice_group_remain),
                "usedPercent": str(round(voice_group_use / total * 100)) if total > 0 else "0",
            }

        if sms_items:
            total_use = sum(_safe_float(i.get("use")) for i in sms_items)
            total_total = sum(_safe_float(i.get("total")) for i in sms_items)
            total_remain = sum(_safe_float(i.get("remain")) for i in sms_items)
            used_pct = round(total_use / total_total * 100) if total_total > 0 else 0
            parsed["sms"] = {
                "use": _fmt_num(total_use),
                "total": _fmt_num(total_total),
                "remain": _fmt_num(total_remain),
                "usedPercent": str(used_pct),
            }
        elif sms_group_use is not None and sms_group_remain is not None:
            total = sms_group_use + sms_group_remain
            parsed["sms"] = {
                "use": _fmt_num(sms_group_use),
                "total": _fmt_num(total),
                "remain": _fmt_num(sms_group_remain),
                "usedPercent": "0",
            }

        return parsed

    async def get_usage_detail(
        self, session: aiohttp.ClientSession
    ) -> dict[str, Any]:
        """从 queryOcsPackageFlowLeftContent API 获取语音、短信、流量详情。

        返回格式：
        {
            "voice": {"use": ..., "total": ..., "remain": ..., "usedPercent": ...},
            "sms": {"use": ..., "total": ..., "remain": ..., "usedPercent": ...},
            "data_items": [
                {"addUpItemName": ..., "use": ..., "total": ..., "remain": ...,
                 "flowType": ..., "beforeTotal": ..., ...},
                ...
            ]
        }

        flowType: 1=通用, 2=专属/App, 3=其他(区域/公免)；total=0 表示不限量。
        """
        try:
            effective_ticket = self._manual_usage_ticket or self._auto_ticket
            effective_phone = self._auto_ticket_phone

            form_data = {
                "duanlianjieabc": "",
                "channelCode": "",
                "serviceType": "",
                "saleChannel": "",
                "externalSources": "",
                "contactCode": "",
                "ticket": effective_ticket,
                "ticketPhone": effective_phone,
                "ticketChannel": "XCXYLCXYY",
                "language": "chinese",
            }

            headers = HEADERS_FORM.copy()
            cookie_header = self._build_cookie_header()
            if cookie_header:
                headers["Cookie"] = cookie_header

            async with session.post(
                API_USAGE_DETAIL, data=form_data, headers=headers
            ) as resp:
                text = await resp.text()
                data = json.loads(text)
                parsed = self._parse_usage_response(data)

                has_data = (
                    bool(parsed.get("data_items"))
                    or bool(parsed.get("voice"))
                    or bool(parsed.get("sms"))
                )

                if has_data:
                    _LOGGER.debug(
                        "用量详情获取: voice=%s, sms=%s, data_items=%d "
                        "(unshared=%s, shareData=%s, resources=%s)",
                        bool(parsed.get("voice")),
                        bool(parsed.get("sms")),
                        len(parsed.get("data_items", [])),
                        bool(data.get("unshared")),
                        bool(data.get("shareData")),
                        bool(data.get("resources")),
                    )
                    return parsed

                _LOGGER.debug("用量详情无有效数据")
                return {}

        except Exception as err:
            _LOGGER.error("获取用量详情失败: %s", err)
            return {}

    async def fetch_all(
        self, session: aiohttp.ClientSession, timeout: float = 15
    ) -> dict[str, Any]:
        """拉取全部数据（概览 + 用量 + 余额 + 账单）。

        Args:
            session: aiohttp.ClientSession 实例
            timeout: 单次请求超时秒数

        Returns:
            {
                "overview": {...},
                "usage_details": {...},
                "balance_detail": {...},
                "bill_detail": {...},
                "last_refresh_time": "YYYY-MM-DD HH:MM:SS"
            }
        """
        result: dict[str, Any] = {
            "overview": {},
            "usage_details": {},
            "balance_detail": {},
            "bill_detail": {},
            "last_refresh_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            # Step 0: 自动鉴权
            await self._auto_get_auth(session)

            # Step 1: 获取概览
            overview = await self.get_overview(session)
            if overview:
                result["overview"] = overview

            # Step 2: 获取余额详情
            balance = await self.get_balance_detail(session)
            if balance:
                result["balance_detail"] = balance

            # Step 3: 获取账单详情
            bill = await self.get_bill_detail(session)
            if bill:
                result["bill_detail"] = bill

            # Step 4: 获取用量详情（语音/短信/流量）
            usage = await self.get_usage_detail(session)
            if usage:
                result["usage_details"] = usage

            return result

        except Exception as err:
            _LOGGER.error("拉取全部数据失败: %s", err)
            return result
