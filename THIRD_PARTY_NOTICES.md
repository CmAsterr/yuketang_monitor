# 依赖与参考说明

本项目通过 Python 包管理器安装第三方依赖，不把第三方浏览器、雨课堂安装包、Office/VSTO、WebDriver 或个人课件打入发行包。下列许可名称来自发布准备时的软件包元数据；具体版权与许可文本以相应依赖包自带文件为准。

| 直接运行依赖 | 许可标识／名称 |
| --- | --- |
| httpx | BSD-3-Clause |
| websockets | BSD-3-Clause |
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic | MIT |
| tomlkit | MIT |
| qrcode | BSD |
| Pillow | MIT-CMU |
| json-repair | MIT |
| markdown-it-py | MIT |
| latex2mathml | MIT |

开发工具 pytest、pytest-asyncio、Ruff、Playwright 和构建工具各自保留其许可；它们不作为应用业务代码打包进 wheel。浏览器测试需要独立安装的 Edge。

## 协议与实现参考

- 雨课堂连接基于本项目已有交接资料及学生端实际接口行为，v5 采用独立 Python 实现。
- QQ 官方机器人协议参考腾讯 QQ 机器人文档，正式实现位于 qq_client.py、qq_store.py 和 qq_service.py；不导入原型网页或完整第三方机器人平台。
- 模型列表的版本路径识别和候选端点思路参考 CC Switch 的 model_fetch.rs，核对的提交为 06082e189d65e6d6dbadc35dacdac1ce6c79d89a；本项目使用独立 Python 实现，没有复制其 Rust 源码。

项目本身经仓库所有者确认采用 MIT 许可证；第三方依赖仍遵循各自的许可。
