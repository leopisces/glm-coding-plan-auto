# GLM Subscriber - 命令手册

## 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器（首次）
playwright install chromium

# 可选：安装 stealth 反检测
pip install playwright-stealth
```

## 运行

```bash
# 离线 OCR 测试（不需要浏览器）
python -m glm_subscriber --test-mode --debug

# 正常运行 - 持久浏览器模式（自动启动 Chrome）
python -m glm_subscriber --browser-mode persistent

# 正常运行 - CDP 模式（连接已运行的 Chrome，需先启动 Chrome 并开启远程调试）
python -m glm_subscriber --browser-mode cdp --cdp-port 9222

# 指定配置文件
python -m glm_subscriber --config config.yaml --browser-mode persistent

# DEBUG 模式
python -m glm_subscriber --browser-mode persistent --debug --log-level DEBUG

# 限制重试次数
python -m glm_subscriber --browser-mode persistent --max-retries 10
```

## 多开

```bash
# 一键多开 3 个实例
python -m glm_subscriber --instances 3 --browser-mode persistent

# 一键多开 5 个实例
python -m glm_subscriber --instances 5 --browser-mode persistent

# 手动单开指定实例
python -m glm_subscriber --instance 1 --browser-mode persistent
python -m glm_subscriber --instance 2 --browser-mode persistent
```

`--instances N` 自动启动 N 个并行进程，实例 ID 从 1 到 N，每个实例间隔 3 秒启动。

`--instance` / `--instances` 自动隔离：

| 资源 | 实例 1 | 实例 2 |
|---|---|---|
| 浏览器数据目录 | `.browser-data-1` | `.browser-data-2` |
| 日志文件 | `logs/glm_subscriber_1.log` | `logs/glm_subscriber_2.log` |
| 控制台前缀 | `[1]` | `[2]` |
| 配置文件 | 优先读 `config_1.yaml`，没有则用 `config.yaml` | 优先读 `config_2.yaml`，没有则用 `config.yaml` |
| CDP 端口 | 基础端口 + 1 | 基础端口 + 2 |

如需每个实例用不同配置（如不同套餐），创建对应的 `config_1.yaml`、`config_2.yaml`。

Ctrl+C 可同时终止所有实例。

## 打包

```bash
# PyInstaller 打包
pyinstaller glm_subscriber.spec --clean --noconfirm

# 产物位于 dist/glm-subscriber/
# 运行前需将 config.yaml 复制到 exe 同级目录
copy config.yaml dist\glm-subscriber\config.yaml
```

## 打包后运行

```bash
# 进入 dist 目录
cd dist\glm-subscriber

# 查看帮助
glm-subscriber.exe --help

# 正常运行
glm-subscriber.exe --browser-mode persistent

# 多开
glm-subscriber.exe --instances 3 --browser-mode persistent

# 手动单开
glm-subscriber.exe --instance 1 --browser-mode persistent

# 离线测试
glm-subscriber.exe --test-mode --debug

# 指定配置
glm-subscriber.exe --config config.yaml --browser-mode persistent

# 指定配置多开
glm-subscriber.exe --instances 2 --config config.yaml --browser-mode persistent
```

## 完整参数

```
--browser-mode {cdp,persistent}   浏览器模式：persistent=自启Chrome, cdp=连接已运行Chrome
--cdp-port CDP_PORT               CDP 端口（默认 9222）
--config CONFIG                   配置文件路径（默认 config.yaml）
--test-mode                       离线 OCR 测试模式
--debug                           保存中间图片到 debug_output/
--max-retries MAX_RETRIES         最大重试次数（默认读配置，-1=无限）
--confidence-threshold FLOAT      最低置信度阈值（默认读配置）
--log-level {DEBUG,INFO,WARNING,ERROR}  日志级别（默认 INFO）
--instance INSTANCE               多开实例 ID，隔离浏览器/日志/配置
--instances INSTANCES             一键启动 N 个并行实例（1..N）
```

## 配置文件说明

主配置文件 `config.yaml`，关键配置项：

- `browser.mode` — 浏览器模式（cdp / persistent）
- `browser.user_data_dir` — 浏览器数据目录（persistent 模式）
- `browser.cdp_port` — CDP 端口
- `selectors.plan` — 套餐选择（Lite / Pro / Max）
- `retry.max_attempts` — 重试次数（-1=无限循环）
- `notification` — 邮件通知配置
- `log` — 日志配置

多开时各实例可使用独立配置：`config_1.yaml`、`config_2.yaml`，只需修改套餐或通知等差异化项即可。

## 测试

```bash
# 运行测试
pytest tests/

# 代码格式化
black glm_subscriber/

# 代码检查
ruff check glm_subscriber/
```
