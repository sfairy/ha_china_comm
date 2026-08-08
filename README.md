# 移动运营商 (china_comm)

Home Assistant 自定义集成，用于查询中国电信 / 中国联通套餐用量与话费信息。

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/sfairy/ha_china_comm)](https://github.com/sfairy/ha_china_comm/releases)

## 功能

- **中国电信**：手机号 + 服务密码登录，查询余额、流量、通话、短信等
- **中国联通**：通过小程序 OpenID 鉴权，查询余额、流量、通话、短信等
- 支持同一 Home Assistant 实例配置多个账号
- 可配置刷新间隔（默认 15 分钟）

## 安装

### 通过 HACS（推荐）

1. 打开 HACS → 集成 → 右上角菜单 → 自定义仓库
2. 仓库地址填写：`https://github.com/sfairy/ha_china_comm`
3. 类别选择 **Integration**
4. 搜索「移动运营商」并安装
5. 重启 Home Assistant
6. 设置 → 设备与服务 → 添加集成 → 搜索「移动运营商」

也可使用 My 链接（需已安装 HACS）：

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sfairy&repository=ha_china_comm&category=integration)

### 手动安装

1. 将本仓库的 `custom_components/china_comm` 目录复制到 Home Assistant 配置目录下的 `custom_components/`
2. 重启 Home Assistant
3. 设置 → 设备与服务 → 添加集成 → 搜索「移动运营商」

## 配置

### 中国电信

| 字段 | 说明 |
|------|------|
| 手机号 | 11 位电信手机号 |
| 服务密码 | 掌上营业厅服务密码（非短信验证码） |
| 设备信任 ID | 可选；设备未信任时可填写 |
| 刷新间隔 | 1–60 分钟 |

提交时会验证登录；失败时根据运营商返回码提示原因。

### 中国联通

| 字段 | 说明 |
|------|------|
| 名称 | 配置项显示名称 |
| OpenID | 联通微信小程序 OpenID（必填） |
| 手机号 | 可选，用于实体标识与标题脱敏 |
| 刷新间隔 | 1–60 分钟 |

OpenID 需自行从联通相关小程序抓包或开发者工具中获取，本集成不提供获取教程以外的第三方账号服务。

配置成功后，可在集成选项中修改上述参数。

## 实体说明

集成会创建传感器实体，常见类型包括：

| 类型 | 说明 | 单位 |
|------|------|------|
| 账户余额 | 当前话费余额 | CNY |
| 本月消费 | 当月已消费金额 | CNY |
| 流量总量 / 已用 / 剩余 | 套餐流量 | GB / MB（视运营商返回） |
| 流量使用率 | 已用占比 | % |
| 通话总量 / 已用 / 剩余 | 套餐语音 | 分钟 |
| 通话使用率 | 已用占比 | % |
| 短信总量 / 已用 / 剩余 | 套餐短信 | 条 |
| 积分 | 账户积分 | — |
| 最近刷新时间 | 上次成功拉取时间 | — |

实体 ID 大致形如 `sensor.ct_xxxx_balance`（电信）或 `sensor.cu_xxxx_balance`（联通）。不同套餐返回字段可能不同，部分实体在无数据时可能不可用。

## 免责声明

- 本集成通过非官方接口查询运营商数据，可能因运营商接口变更而失效
- 请妥善保管服务密码与 OpenID，勿提交到公开仓库
- 使用本集成产生的账号风险由使用者自行承担
- 图标与名称不代表对中国电信、中国联通等品牌的官方背书

## 问题反馈

请在 [GitHub Issues](https://github.com/sfairy/ha_china_comm/issues) 提交问题。

## 许可证

[MIT](LICENSE)
