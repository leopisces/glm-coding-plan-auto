# GLM Auto-Subscribe

GLM Coding 套餐自动订阅工具。自动识别腾讯点选验证码，循环尝试直至抢购成功并通知。

![效果图](动画.gif)

## 功能

- 自动启动浏览器并导航到目标页面
- OCR 识别腾讯点选验证码（RapidOCR + ONNX Runtime）
- 模糊匹配相似字形，提高识别率
- 多轮重试 + 自动刷新过期验证码
- 抢购成功后邮件通知（QQ 邮箱 SMTP）
- 支持多开并行，一键启动 N 个实例
- 打包为独立 exe，无需 Python 环境即可运行

## 项目结构

```
glm_subscriber/
├── main.py              # 入口，CLI 参数解析，主循环
├── browser.py           # 浏览器连接（CDP / 持久模式），页面操作
├── captcha_capture.py   # 验证码截图，区域裁剪
├── captcha_solver.py    # 验证码求解：目标提取、OCR、坐标映射、点击
├── rapidocr_engine.py   # RapidOCR 引擎封装
├── orchestrator.py      # 重试编排，错误分类与退避
├── notify.py            # 邮件通知
├── dom_analyzer.py      # DOM 分析
├── types.py             # 数据类型定义
config.yaml              # 配置文件
glm_subscriber.spec      # PyInstaller 打包规格
```

## 环境准备

```bash
pip install -r requirements.txt
playwright install chromium

# 可选：反自动化检测
pip install playwright-stealth
```

## 运行

```bash
# 离线 OCR 测试
python -m glm_subscriber --test-mode --debug

# 正常运行（自动启动 Chrome，导航到配置中的 URL）
python -m glm_subscriber --browser-mode persistent

# CDP 模式（连接已运行的 Chrome）
python -m glm_subscriber --browser-mode cdp --cdp-port 9222

# 指定配置文件
python -m glm_subscriber --config config.yaml --browser-mode persistent

# 调试模式
python -m glm_subscriber --browser-mode persistent --debug --log-level DEBUG
```

## 多开

```bash
# 一键开 3 个实例
python -m glm_subscriber --instances 3 --browser-mode persistent

# 手动指定实例 ID
python -m glm_subscriber --instance 1 --browser-mode persistent
python -m glm_subscriber --instance 2 --browser-mode persistent
```

`--instances N` 自动启动 N 个并行进程，每个实例自动隔离：

| 资源 | 实例 1 | 实例 2 |
|---|---|---|
| 浏览器数据目录 | `.browser-data-1` | `.browser-data-2` |
| 日志文件 | `logs/glm_subscriber_1.log` | `logs/glm_subscriber_2.log` |
| 控制台前缀 | `[1]` | `[2]` |
| 配置文件 | 优先读 `config_1.yaml` | 优先读 `config_2.yaml` |
| CDP 端口 | 基础端口 + 1 | 基础端口 + 2 |

如需各实例用不同配置（如不同套餐），创建 `config_1.yaml`、`config_2.yaml` 即可。Ctrl+C 同时终止所有实例。

## 打包

```bash
pyinstaller glm_subscriber.spec --clean --noconfirm

# 将 config.yaml 复制到 exe 同级目录
copy config.yaml dist\glm-subscriber\config.yaml
```

## 打包后运行

```bash
cd dist\glm-subscriber

glm-subscriber.exe --browser-mode persistent
glm-subscriber.exe --instances 3 --browser-mode persistent
glm-subscriber.exe --test-mode --debug
```

## 配置文件

编辑 `config.yaml`，关键配置项：

```yaml
browser:
  mode: "persistent"
  url: "https://www.bigmodel.cn/glm-coding?ic=QJ8SDUDGOX"  # 启动后自动导航
  user_data_dir: ".browser-data"
  headless: false

selectors:
  plan: "Pro"  # Lite / Pro / Max

retry:
  max_attempts: -1  # -1 = 无限循环

notification:
  enabled: true
  smtp_host: "smtp.qq.com"
  smtp_port: 465
  sender: "xxx@qq.com"
  password: "授权码"
  receiver: "xxx@qq.com"
```

打包后修改 exe 同级目录的 `config.yaml` 即可生效，无需重新打包。

## 完整参数

| 参数 | 说明 |
|---|---|
| `--browser-mode {cdp,persistent}` | 浏览器模式：persistent=自启 Chrome，cdp=连接已运行 Chrome |
| `--cdp-port PORT` | CDP 端口（默认 9222） |
| `--config FILE` | 配置文件路径（默认 config.yaml） |
| `--test-mode` | 离线 OCR 测试模式 |
| `--debug` | 保存中间图片到 debug_output/ |
| `--max-retries N` | 最大重试次数（-1=无限） |
| `--confidence-threshold F` | 最低置信度阈值 |
| `--log-level LEVEL` | 日志级别：DEBUG/INFO/WARNING/ERROR |
| `--instance ID` | 多开实例 ID，隔离浏览器/日志/配置 |
| `--instances N` | 一键启动 N 个并行实例 |
