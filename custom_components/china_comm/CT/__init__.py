"""
移动运营商 - 中国电信子模块 (CT)

本模块独立封装中国电信的套餐查询逻辑：

- api.py：封装 userLoginNormal、qryImportantData 接口，RSA+凯撒加密，自定义 SSL 适配器
- sensor.py：创建余额、流量、通话、积分等传感器实体，支持 token 过期自动重登

鉴权：手机号 + 服务密码。通过 __all__ 暴露 async_setup_telecom。
"""

from .sensor import async_setup_telecom

__all__ = ["async_setup_telecom"]
