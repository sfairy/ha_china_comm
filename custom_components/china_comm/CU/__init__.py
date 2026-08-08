"""
移动运营商 - 中国联通子模块 (CU)

本模块独立封装中国联通的套餐查询逻辑：

- api.py：封装 mina.10010.com 小程序 API（getTicket、sspbigball、余额、账单、用量）
- sensor.py：通过 api 获取语音、短信、流量及余额数据，创建传感器实体

鉴权：OpenID（从联通小程序获取），无需服务密码。通过 __all__ 暴露 async_setup_unicom。
"""

from .sensor import async_setup_unicom

__all__ = ["async_setup_unicom"]
