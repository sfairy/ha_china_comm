"""
中国电信 - API 封装模块

封装中国电信掌上营业厅 App 的 HTTP 接口，包括：
- 用户登录（userLoginNormal）
- 套餐重要数据查询（qryImportantData）
- 数据解析与汇总（to_summary）

安全与兼容性处理：
- 密码使用 RSA 公钥加密
- 手机号使用凯撒移位（+2/-2）加密
- 自定义 SSL 适配器解决与电信服务器的 TLS 握手兼容问题
- 日志脱敏：自动屏蔽手机号、密码、token 等敏感信息
"""

import re
import base64
import json
import requests
from datetime import datetime
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import logging
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import certifi

from ..utils import convert_flow as _convert_flow

# 日志记录器，用于输出 API 请求、错误等信息
_LOGGER = logging.getLogger(__name__)


class TelecomSSLAdapter(HTTPAdapter):
    """
    自定义 HTTP 适配器，解决与电信服务器 SSL 握手兼容问题。

    部分环境下 OpenSSL 默认安全级别较高，会导致与电信 API 的 TLS 握手失败。
    通过降低 cipher 安全级别（SECLEVEL=1）并使用 certifi 证书，提高兼容性。
    """

    def __init__(self):
        # 延迟初始化 SSL 上下文，在首次创建连接池时再创建
        self.ssl_context = None
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        """初始化连接池管理器，配置自定义 SSL 上下文。"""
        if self.ssl_context is None:
            # 创建 urllib3 的 SSL 上下文
            self.ssl_context = create_urllib3_context()
            # 设置加密套件，SECLEVEL=1 允许部分较旧的加密算法，提高与电信服务器的兼容性
            self.ssl_context.set_ciphers("DEFAULT:@SECLEVEL=1")
            # 使用 certifi 提供的 CA 证书进行服务器证书验证
            self.ssl_context.load_verify_locations(cafile=certifi.where())
        pool_kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)


class Telecom:
    """
    中国电信 API 客户端。

    负责登录、查询套餐数据，并将原始 API 响应解析为结构化摘要数据。
    所有与电信服务器的通信均通过此类完成。
    """

    def __init__(self):
        """初始化 API 客户端，配置请求会话和 HTTP 头。"""
        # 登录成功后保存的完整信息：token、省市区编码、设备信息等
        self.login_info = {}
        # 当前登录的手机号码
        self.phonenum = None
        # 电信服务密码（用于登录掌上营业厅）
        self.password = None
        # 登录后获得的 token，后续查询请求需携带此 token 进行鉴权
        self.token = None
        # 客户端类型标识，模拟电信掌上营业厅 App，用于通过服务器校验
        self.login_client_type = "#12.2.0#channel50#iPhone 14 Pro#"
        self.query_client_type = "#12.2.0#channel50#iPhone 14 Pro#"
        self.client_type = self.query_client_type
        # 请求头，模拟 App 的 JSON 请求格式
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        # 创建持久化会话，复用 TCP 连接
        self.session = requests.Session()
        # 挂载自定义 SSL 适配器到 HTTPS 协议
        adapter = TelecomSSLAdapter()
        self.session.mount("https://", adapter)
        # 使用 certifi 证书验证服务器
        self.session.verify = certifi.where()

    def _mask_value(self, value):
        """
        对敏感值进行脱敏处理。

        替换手机号、密码及其编码形式，保护用户隐私。

        Args:
            value: 待脱敏的值

        Returns:
            脱敏后的字符串
        """
        if value is None:
            return None
        text = str(value)
        if self.phonenum:
            text = text.replace(self.phonenum, f"{self.phonenum[:3]}****{self.phonenum[7:]}")
            text = text.replace(self.trans_number(self.phonenum), "***encoded_phone***")
        if self.password:
            text = text.replace(self.password, "***password***")
            text = text.replace(self.trans_number(self.password), "***encoded_password***")
        text = re.sub(r"\b(1[3-9]\d)\d{4}(\d{4})\b", r"\1****\2", text)
        if len(text) <= 8:
            return "***"
        return f"{text[:4]}***{text[-4:]}"

    def sanitize_for_log(self, data):
        """
        递归脱敏字典/列表中的敏感字段。

        自动识别并脱敏 account、password、token、phone 等关键字段。

        Args:
            data: 待脱敏的数据（字典、列表或字符串）

        Returns:
            脱敏后的数据
        """
        sensitive_keys = {
            "account",
            "androidid",
            "authentication",
            "deviceid",
            "deviceuid",
            "loginauthcipherasymmertric",
            "password",
            "phonenum",
            "phoneNum",
            "sharephonenum",
            "token",
            "userId",
            "userid",
            "userloginname",
        }
        if isinstance(data, dict):
            sanitized = {}
            for key, value in data.items():
                key_lower = str(key).lower()
                if key_lower in sensitive_keys or "phone" in key_lower or "token" in key_lower:
                    sanitized[key] = self._mask_value(value)
                else:
                    sanitized[key] = self.sanitize_for_log(value)
            return sanitized
        if isinstance(data, list):
            return [self.sanitize_for_log(item) for item in data]
        if isinstance(data, str):
            text = data
            if self.phonenum:
                text = text.replace(self.phonenum, f"{self.phonenum[:3]}****{self.phonenum[7:]}")
                text = text.replace(self.trans_number(self.phonenum), "***encoded_phone***")
            if self.password:
                text = text.replace(self.password, "***password***")
                text = text.replace(self.trans_number(self.password), "***encoded_password***")
            return re.sub(r"\b(1[3-9]\d)\d{4}(\d{4})\b", r"\1****\2", text)
        return data

    def format_for_log(self, data):
        """
        将数据格式化为安全的日志字符串。

        对敏感字段脱敏后，转换为 JSON 字符串。

        Args:
            data: 待格式化的数据

        Returns:
            脱敏后的 JSON 字符串
        """
        try:
            return json.dumps(self.sanitize_for_log(data), ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(self.sanitize_for_log(str(data)))

    def _response_json(self, response, context):
        """
        处理 API 响应，转换为 JSON 并记录日志。

        若响应非 JSON，记录错误信息并返回错误字典。

        Args:
            response: requests.Response 对象
            context: 请求上下文描述（如 "login"、"qryImportantData"）

        Returns:
            JSON 响应数据或错误字典
        """
        try:
            payload = response.json()
        except ValueError:
            payload = {
                "error": "non_json_response",
                "status_code": response.status_code,
                "text": response.text,
            }
            _LOGGER.error(
                "China Telecom %s returned non-JSON response: status=%s headers=%s body=%s",
                context,
                response.status_code,
                self.format_for_log(dict(response.headers)),
                self.format_for_log(response.text),
            )
            return payload

        _LOGGER.debug(
            "China Telecom %s response: status=%s body=%s",
            context,
            response.status_code,
            self.format_for_log(payload),
        )
        return payload

    def set_login_info(self, login_info):
        """
        设置登录信息，用于后续查询请求的鉴权。

        Args:
            login_info: 登录成功后 API 返回的完整数据字典，包含 phonenum、password、token 等
        """
        self.login_info = login_info
        self.phonenum = login_info.get("phonenum", None)
        self.password = login_info.get("password", None)
        self.token = login_info.get("token", None)

    def trans_number(self, phonenum, encode=True):
        """
        凯撒移位加密/解密手机号，偏移量为 2。

        电信 API 要求对手机号、密码等敏感字段进行简单加密后传输。
        encode=True 时每个字符 ASCII 码 +2（加密），False 时 -2（解密）。
        使用 & 65535 确保结果在 Unicode 基本平面内。

        Args:
            phonenum: 待加密或解密的字符串（通常为手机号或密码）
            encode: True 表示加密，False 表示解密

        Returns:
            加密或解密后的字符串
        """
        result = ""
        caesar_size = 2 if encode else -2
        for char in phonenum:
            result += chr(ord(char) + caesar_size & 65535)
        return result

    def encrypt(self, str):
        """
        使用 RSA 公钥加密字符串，用于加密登录请求中的敏感信息。

        电信 API 要求将复合登录参数（设备信息+手机号+时间戳+密码）用其公钥加密后传输。
        公钥为电信掌上营业厅官方公钥，加密算法为 PKCS1_v1_5。

        Args:
            str: 待加密的明文字符串

        Returns:
            Base64 编码后的密文字符串
        """
        public_key_pem = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofd
WzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18
qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMi
PMpq0/XKBO8lYhN/gwIDAQAB
-----END PUBLIC KEY-----"""
        public_key = RSA.import_key(public_key_pem.encode())
        cipher = PKCS1_v1_5.new(public_key)
        ciphertext = cipher.encrypt(str.encode())
        encoded_ciphertext = base64.b64encode(ciphertext).decode()
        return encoded_ciphertext

    def get_fee_flow_limit(self, fee_remain_flow):
        """
        计算每日可用流量限额（基于剩余天数平均分配）。

        Args:
            fee_remain_flow: 剩余流量（单位 KB）

        Returns:
            每日可用流量限额（整数，单位 KB）
        """
        today = datetime.today()
        days_in_month = (
            datetime(today.year, today.month + 1, 1)
            - datetime(today.year, today.month, 1)
        ).days
        return int((fee_remain_flow / days_in_month))

    def user_flux_package(self, **kwargs):
        """
        查询流量包详情。

        Args:
            **kwargs: 可传入 billing_cycle（账期，格式 YYYYMM）和 token

        Returns:
            流量包查询 API 的原始 JSON 响应
        """
        billing_cycle = kwargs.get("billing_cycle") or datetime.now().strftime("%Y%m")
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "fieldData": {
                    "queryFlag": "0",
                    "accessAuth": "1",
                    "account": self.trans_number(self.phonenum),
                },
                "attach": "test",
            },
            "headerInfos": {
                "code": "userFluxPackage",
                "clientType": self.client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(self.phonenum),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/userFluxPackage",
                headers=self.headers,
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            return self._response_json(response, "userFluxPackage")
        except requests.exceptions.RequestException as e:
            _LOGGER.error("查询流量包失败: %s", e)
            return {}

    def qry_share_usage(self, **kwargs):
        """
        查询共享套餐使用情况。

        Args:
            **kwargs: 可传入 billing_cycle（账期，格式 YYYYMM）和 token

        Returns:
            共享使用情况 API 的原始 JSON 响应，号码字段已解密
        """
        billing_cycle = kwargs.get("billing_cycle") or datetime.now().strftime("%Y%m")
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        body = {
            "content": {
                "attach": "test",
                "fieldData": {
                    "billingCycle": billing_cycle,
                    "account": self.trans_number(self.phonenum),
                },
            },
            "headerInfos": {
                "code": "qryShareUsage",
                "clientType": self.client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(self.phonenum),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/qryShareUsage",
                headers=self.headers,
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            data = self._response_json(response, "qryShareUsage")
            # 返回的号码字段加密，需做解密转换
            if data.get("responseData") and data.get("responseData").get(
                "data", {}
            ).get("sharePhoneBeans", []):
                for item in data["responseData"]["data"]["sharePhoneBeans"]:
                    item["sharePhoneNum"] = self.trans_number(item["sharePhoneNum"], False)
                for share_type in data["responseData"]["data"]["shareTypeBeans"]:
                    for share_info in share_type["shareUsageInfos"]:
                        for share_amount in share_info["shareUsageAmounts"]:
                            share_amount["phoneNum"] = self.trans_number(
                                share_amount["phoneNum"], False
                            )
            return data
        except requests.exceptions.RequestException as e:
            _LOGGER.error("查询共享使用情况失败: %s", e)
            return {}

    def do_login(self, phonenum, password, telecom_device_id=""):
        """
        执行电信账号登录，获取 token 用于后续套餐查询。

        登录流程：
        1. 生成设备 UUID（基于手机号，保持稳定）和时间戳
        2. 构建复合字符串（设备型号+UUID前12位+手机号+时间戳+密码）
        3. 使用 RSA 公钥加密复合字符串
        4. 使用凯撒移位加密手机号和密码
        5. 发送 POST 请求到电信登录接口

        若 SSL 握手失败，会降级为 verify=False 重试（兼容部分环境）。

        Args:
            phonenum: 11 位手机号码
            password: 电信服务密码（掌上营业厅登录密码）
            telecom_device_id: 设备信任 ID（可选，用于设备绑定授权）

        Returns:
            登录 API 的 JSON 响应，成功时包含 responseData.loginSuccessResult
        """
        phonenum = str(phonenum or self.phonenum)
        password = str(password or self.password)
        telecom_device_id = str(telecom_device_id or "").strip()
        self.phonenum = phonenum
        self.password = password
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        system_version = "15.4.0"
        # Keep server-side device identity stable. Do not generate a random id per login.
        device_uid = f"3{phonenum}"
        trusted_device_for_sign = telecom_device_id[:12] if telecom_device_id else phonenum
        enc_str = (
            f"iPhone 14 {system_version}"
            f"{trusted_device_for_sign}{phonenum}{ts}{password}0$$$0."
        )
        body = {
            "content": {
                "fieldData": {
                    "accountType": "",
                    "authentication": self.trans_number(password),
                    "deviceUid": device_uid,
                    "isChinatelecom": "0",
                    "loginAuthCipherAsymmertric": self.encrypt(enc_str),
                    "loginType": "4",
                    "phoneNum": self.trans_number(phonenum),
                    "systemVersion": system_version,
                    "androidId": self.trans_number(telecom_device_id) if telecom_device_id else "",
                },
                "attach": "iPhone",
            },
            "headerInfos": {
                "code": "userLoginNormal",
                "clientType": self.login_client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(phonenum),
            },
        }
        try:
            response = self.session.post(
                "https://appgologin.189.cn:9031/login/client/userLoginNormal",
                headers=self.headers,
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            return self._response_json(response, "login")
        except requests.exceptions.SSLError as ssl_err:
            _LOGGER.error(f"SSL验证失败: {ssl_err}")
            _LOGGER.warning("尝试临时禁用SSL验证...")
            # 使用独立的会话临时禁用验证
            temp_session = requests.Session()
            response = temp_session.post(
                "https://appgologin.189.cn:9031/login/client/userLoginNormal",
                headers=self.headers,
                json=body,
                verify=False,
                timeout=30,
            )
            response.raise_for_status()
            return self._response_json(response, "login without SSL verification")
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error(f"请求失败: {req_err}")
            result = {"error": str(req_err)}
            response = getattr(req_err, "response", None)
            if response is not None:
                result["status_code"] = response.status_code
                result["text"] = response.text
            return result

    def qry_important_data(self, **kwargs):
        """
        查询套餐重要数据（流量、语音、余额、积分等）。

        需先调用 do_login 获取 token，本方法在请求头中携带 token 进行鉴权。
        返回原始 API 响应（JSON），需配合 to_summary 方法解析为结构化数据。

        Args:
            **kwargs: 可传入 token 覆盖 self.token，用于重新登录后的 token 更新

        Returns:
            电信 API 的原始 JSON 响应，成功时包含 responseData.data
        """
        # 时间戳精确到分钟，部分接口要求此格式
        ts = datetime.now().strftime("%Y%m%d%H%M00")
        account = self.phonenum
        body = {
            "content": {
                "fieldData": {
                    "provinceCode": self.login_info.get("provinceCode") or "600101",
                    "cityCode": self.login_info.get("cityCode") or "8441900",
                    "shopId": "20002",
                    "isChinatelecom": "0",
                    "account": self.trans_number(account),
                }
            },
            "headerInfos": {
                "code": "qryImportantData",
                "clientType": self.query_client_type,
                "timestamp": ts,
                "shopId": "20002",
                "source": "110003",
                "sourcePassword": "Sid98s",
                "userLoginName": self.trans_number(account),
                "token": kwargs.get("token") or self.token,
            },
        }
        try:
            response = self.session.post(
                "https://appfuwu.189.cn:9021/query/qryImportantData",
                headers=self.headers,
                json=body,
                timeout=30,
            )
            response.raise_for_status()
            return self._response_json(response, "qryImportantData")
        except requests.exceptions.RequestException as e:
            _LOGGER.error("查询重要数据失败: %s", e)
            result = {"error": str(e)}
            response = getattr(e, "response", None)
            if response is not None:
                result["status_code"] = response.status_code
                result["text"] = response.text
            return result

    @staticmethod
    def _to_number(value, default=0):
        """
        将值转换为数字，处理 None、布尔值、字符串等情况。

        Args:
            value: 待转换的值
            default: 转换失败时的默认值

        Returns:
            转换后的浮点数或默认值
        """
        if value is None or isinstance(value, bool):
            return default
        try:
            if isinstance(value, str):
                value = value.strip().replace(",", "")
                if not value:
                    return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def to_summary(self, data, phonenum=""):
        """
        将 qryImportantData 的原始响应解析为结构化摘要。

        从电信 API 的嵌套 JSON 中提取并标准化以下数据：
        - 流量：总量、已用、剩余、超量（单位 KB）
        - 通用流量、专用流量
        - 语音：已用、剩余、总量（单位 分钟）
        - 余额、本月消费（单位 分，1元=100分）
        - 积分
        - 流量子项列表（主套餐、定向流量等）

        Args:
            data: qryImportantData 返回的 responseData.data 部分
            phonenum: 手机号，用于摘要中的标识

        Returns:
            标准化后的摘要字典，供传感器平台使用
        """
        if not data:
            return {}
        phonenum = phonenum or self.phonenum

        # 从原始数据中提取各模块
        flow_info = data.get("flowInfo") or {}
        voice_info = data.get("voiceInfo") or {}
        sms_info = data.get("smsInfo") or {}
        integral_info = data.get("integralInfo") or {}

        # 解析总流量：已用、剩余、总量、超量（单位 KB）
        total_amount = flow_info.get("totalAmount") or {}
        flow_use = int(total_amount.get("used") or 0)
        flow_balance = int(total_amount.get("balance") or 0)
        flow_total = flow_use + flow_balance
        flow_over = int(total_amount.get("over") or 0)

        # 解析通用流量（不含定向流量）
        common_flow = flow_info.get("commonFlow") or {}
        common_use = int(common_flow.get("used") or 0)
        common_balance = int(common_flow.get("balance") or 0)
        common_total = common_use + common_balance
        common_over = int(common_flow.get("over") or 0)

        # 解析专用流量（定向流量，如视频、音乐等）
        special_amount = flow_info.get("specialAmount") or {}
        special_use = int(special_amount.get("used") or 0)
        special_balance = int(special_amount.get("balance") or 0)
        special_total = special_use + special_balance

        # 解析语音通话：已用、剩余、总量（单位 分钟）
        voice_data_info = voice_info.get("voiceDataInfo") or {}
        voice_usage = int(voice_data_info.get("used") or 0)
        voice_balance = int(voice_data_info.get("balance") or 0)
        voice_total = int(voice_data_info.get("total") or 0)

        # 解析短信：优先 smsInfo，否则从 flowList 中查找含「短信」的项（单位 条）
        def _parse_sms_count(s):
            if s is None:
                return 0
            s = str(s).strip()
            m = re.search(r"[\d.]+", s.replace("条", "").replace("，", ""))
            return int(float(m.group(0))) if m else 0

        sms_usage, sms_balance, sms_total = 0, 0, 0
        sms_data_info = sms_info.get("smsDataInfo") or sms_info.get("dataInfo") or {}
        if sms_data_info:
            sms_usage = int(sms_data_info.get("used") or sms_data_info.get("usedAmount") or 0)
            sms_balance = int(sms_data_info.get("balance") or sms_data_info.get("remainAmount") or 0)
            sms_total = int(sms_data_info.get("total") or sms_data_info.get("totalAmount") or 0) or (sms_usage + sms_balance)
        else:
            for item in (flow_info.get("flowList") or []):
                if not isinstance(item, dict):
                    continue
                if "短信" not in item.get("title", ""):
                    continue
                if "已用" in item.get("leftTitle", "") and "剩余" in item.get("rightTitle", ""):
                    sms_usage = _parse_sms_count(item.get("leftTitleHh", "0"))
                    sms_balance = _parse_sms_count(item.get("rightTitleHh", "0"))
                    sms_total = sms_usage + sms_balance
                break

        # 解析余额：balance 为可用余额，arrear 为欠费，统一转为分（1元=100分）
        balance_info = data.get("balanceInfo") or {}
        balance_info_data = balance_info.get("indexBalanceDataInfo") or {}
        balance_str = balance_info_data.get("balance", "0.00")
        arrear_str = balance_info_data.get("arrear", "0.00")

        # 将字符串转换为浮点数进行比较和计算
        balance_float = self._to_number(balance_str)
        arrear_float = self._to_number(arrear_str)

        # 逻辑：如果余额为0且有欠费，则将余额设置为负的欠费值
        if balance_float == 0.00 and arrear_float > 0.00:
            balance = -int(round(arrear_float * 100))
        else:
            balance = int(round(balance_float * 100))
        arrear = int(round(arrear_float * 100))  # 欠费，单位分

        # 本月消费金额，从话费区域 subTitleHh 解析（如 "128.50元"）
        phone_bill_region = balance_info.get("phoneBillRegion") or {}
        if not isinstance(phone_bill_region, dict):
            phone_bill_region = {}
        current_month_cost_str = str(phone_bill_region.get("subTitleHh") or "0元").replace('元', '')
        try:
            current_month_cost = int(round(float(current_month_cost_str) * 100))  # 转为分
        except ValueError:
            current_month_cost = 0

        # 积分
        points = int(self._to_number(integral_info.get("integral")))

        # 解析流量子项列表（如主套餐流量、定向流量、加油包等）
        flow_items = []
        flow_lists = flow_info.get("flowList", [])
        for item in flow_lists:
            if not isinstance(item, dict):
                continue
            if "流量" not in item.get("title", ""):
                continue
            # 根据 leftTitle/rightTitle 格式解析已用、剩余、总量（不同子项格式可能不同）
            if "已用" in item.get("leftTitle", "") and "剩余" in item.get("rightTitle", ""):
                # 标准格式：左为已用，右为剩余
                item_use = self.convert_flow(item.get("leftTitleHh", "0KB"), "KB")
                item_balance = self.convert_flow(item.get("rightTitleHh", "0KB"), "KB")
                item_total = item_use + item_balance
            elif "超出" in item.get("leftTitle", "") and "/" in item.get("rightTitleEnd", ""):
                # 超出流量格式：左为超出量，右为 "已用/总量"
                item_balance = -self.convert_flow(item.get("leftTitleHh", "0KB"), "KB")
                item_use = self.convert_flow(item.get("rightTitleEnd", "0/0").split("/")[1], "KB") - item_balance
                item_total = item_use + item_balance
            elif "已用" in item.get("leftTitle", "") and "降速" in item.get("rightTitle", ""):
                # 降速流量格式：右标题含总量（如 "20GB后降速"）
                match = re.search(r"(\d+[KMGT]B)", item.get("rightTitle", ""))
                item_total = self.convert_flow(match.group(1), "KB") if match else 0
                item_use = self.convert_flow(item.get("leftTitleHh", "0KB"), "KB")
                item_balance = item_total - item_use
            else:
                continue
            flow_items.append(
                {"name": item.get("title", ""), "use": item_use, "balance": item_balance, "total": item_total}
            )

        return {
            "phonenum": phonenum,
            "balance": balance,
            "arrear": arrear,
            "currentMonthCost": current_month_cost,
            "voiceUsage": voice_usage,
            "voiceBalance": voice_balance,
            "voiceTotal": voice_total,
            "smsUsage": sms_usage,
            "smsBalance": sms_balance,
            "smsTotal": sms_total,
            "flowUse": flow_use,
            "flowTotal": flow_total,
            "flowOver": flow_over,
            "commonUse": common_use,
            "commonTotal": common_total,
            "commonOver": common_over,
            "specialUse": special_use,
            "specialTotal": special_total,
            "points": points,
            "createTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "flowItems": flow_items,
        }

    def convert_flow(self, size_str, target_unit="KB", decimal=0):
        """将流量字符串或数值转换为指定单位，委托 utils.convert_flow。"""
        return _convert_flow(size_str, target_unit, decimal)