# monitor_reminder

> 监控远程服务器的计算任务（如 Gaussian、VASP、ORCA 等），计算完成时自动发送邮件通知。

🔗 [GitHub 仓库](https://github.com/TungsticAcid/monitor_reminder)

## 检测逻辑

1. 同时监控多台服务器
2. 每台服务器 SSH → `ps aux` 汇总目标进程的 %CPU
3. CPU > 阈值 → 该服务器还在算；CPU ≤ 阈值 → 确认 log 是否写完
4. 某台服务器 CPU 降至阈值以下且 log 确认完成 → 发送邮件通知
5. 确认后该服务器不再提醒，直到 CPU 再次超过阈值（有新作业）

## 快速开始

### 1. 安装依赖

```bash
pip install paramiko
```

邮件发送使用 Python 标准库 `smtplib`，无需额外安装。

### 2. 编辑配置文件

编辑 `config.json`，替换所有 `your_` 开头的占位符为实际值：

```json
{
  "smtp": {
    "server": "smtp.qq.com",
    "username": "your_email@qq.com",
    "password": "your_smtp_auth_code"
  },
  "alert": {
    "recipients": ["your_email@qq.com"]
  },
  "monitor": {
    "monitor_list": ["server_1"],
    "servers": {
      "server_1": {
        "host": "your_server_ip",
        "user": "your_username",
        "pass": "your_password",
        "dir": "/path/to/workdir/"
      }
    }
  }
}
```

### 3. 测试邮件配置

```bash
python monitor_reminder.py --dry-run
```

### 4. 启动监控

```bash
python monitor_reminder.py                      # 按配置中的间隔循环监控
python monitor_reminder.py --interval 120       # 临时覆盖：每 2 分钟检查
python monitor_reminder.py --once               # 只检查一次，立即退出
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `--interval N`, `-i N` | 临时覆盖检查间隔（秒） |
| `--once` | 只检查一次，立即退出 |
| `--dry-run` | 发送一封测试邮件验证配置，然后退出 |
| `--config PATH`, `-c PATH` | 指定配置文件路径（默认 `config.json`） |

## 配置文件说明

### smtp — SMTP 服务器

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `server` | string | 是 | SMTP 服务器地址 |
| `port` | int | 是 | 端口（TLS: 587, SSL: 465） |
| `use_tls` | bool | 否 | 是否 STARTTLS（默认 true） |
| `username` | string | 是 | 登录邮箱 |
| `password` | string | 是 | SMTP 授权码 |
| `sender_name` | string | 否 | 发件人显示名称 |

### alert — 告警设置

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `recipients` | string[] | 是 | 收件人列表 |
| `cc` | string[] | 否 | 抄送列表 |
| `subject_template` | string | 否 | 主题模板 |
| `body_template` | string | 否 | 正文模板 |
| `body_type` | string | 否 | `plain`（默认）或 `html` |

**模板变量**：`{name}` `{total}` `{normal}` `{error}` `{time}` `{datetime}` `{date}`

### monitor — 监控设置

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `check_interval` | int | 否 | 检查间隔（秒），默认 200 |
| `cpu_threshold` | float | 否 | CPU 阈值（%），默认 200 |
| `check_timeout` | int | 否 | SSH 超时（秒），默认 60 |
| `monitor_list` | string[] | 是 | 要监控的服务器名称列表 |
| `servers` | object | 是 | 服务器连接信息（key=名称） |

**servers 中每台服务器的字段**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `host` | string | 是 | 服务器 IP/域名 |
| `port` | int | 否 | SSH 端口（默认 22） |
| `user` | string | 是 | SSH 用户名 |
| `pass` | string/null | 否 | SSH 密码（null=运行时交互输入） |
| `dir` | string | 是 | 工作目录 |
| `procs` | string[] | 否 | `ps aux` 中要匹配的进程名（如 `["g09", "vasp"]`），用于区分计算进程 |
| `thresh` | float/null | 否 | 该服务器的 CPU 阈值（null=使用全局 `cpu_threshold`） |
| `nproc` | int/null | 否 | CPU 核数（null=自动检测） |

## 文件结构

```
├── email_utils.py            # 邮件发送核心模块
├── monitor_reminder.py       # 监控主程序
├── config.json              # 配置文件（SMTP + 告警 + 监控）
└── README.md                # 本文档
```

## License

MIT
