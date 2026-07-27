#!/usr/bin/env python
"""
邮件发送核心模块
================
提供邮件构建、SMTP 发送、配置加载与校验功能。
纯标准库实现 SMTP 通信，无额外依赖。

功能：
  - load_config()     : 读取并校验 JSON 配置文件
  - validate_config() : 配置完整性校验，返回问题列表
  - send_email()      : 发送单封邮件，支持纯文本和 HTML 格式
  - send_email_with_retry() : 带重试机制的邮件发送
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any

# ===================== 日志 =====================
logger = logging.getLogger("email_utils")


# ===================== 配置加载 =====================

def load_config(config_path: str = "config.json") -> dict[str, Any]:
    """读取 JSON 配置文件并校验。

    Args:
        config_path: 配置文件路径，默认为当前目录下的 config.json

    Returns:
        解析后的配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        json.JSONDecodeError: JSON 格式错误
        ValueError: 配置内容不合法
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.absolute()}")

    with open(path, "r", encoding="utf-8") as f:
        config: dict[str, Any] = json.load(f)

    # 校验配置完整性
    errors = validate_config(config)
    if errors:
        err_msg = "配置校验失败:\n  - " + "\n  - ".join(errors)
        raise ValueError(err_msg)

    logger.info(f"配置加载成功: {path.absolute()}，共 {len(config.get('emails', []))} 个邮件任务")
    return config


def validate_config(config: dict[str, Any]) -> list[str]:
    """校验配置完整性，返回问题列表。空列表表示配置合法。

    Args:
        config: 配置字典

    Returns:
        错误信息列表，若为空则表示校验通过
    """
    errors: list[str] = []

    # 1. 校验 smtp 节
    smtp = config.get("smtp")
    if not smtp:
        errors.append("缺少 'smtp' 配置节")
    else:
        required_smtp = ["server", "port", "username", "password"]
        for key in required_smtp:
            if not smtp.get(key):
                errors.append(f"smtp.{key} 不能为空")
        # port 必须是整数
        port = smtp.get("port")
        if port is not None and not isinstance(port, int):
            errors.append(f"smtp.port 必须是整数，当前值: {port}")

    # 2. 校验 emails 节
    emails = config.get("emails")
    if not emails:
        errors.append("缺少 'emails' 配置节，至少需要配置一个邮件任务")
    elif not isinstance(emails, list):
        errors.append("'emails' 必须是数组")
    else:
        for i, email_cfg in enumerate(emails):
            prefix = f"emails[{i}]"
            if not email_cfg.get("subject"):
                errors.append(f"{prefix}.subject 不能为空")
            if not email_cfg.get("recipients"):
                errors.append(f"{prefix}.recipients 不能为空，至少需要一个收件人")
            if not isinstance(email_cfg.get("recipients"), list):
                errors.append(f"{prefix}.recipients 必须是数组")
            # 校验 schedule
            schedule_cfg = email_cfg.get("schedule")
            if not schedule_cfg:
                errors.append(f"{prefix}.schedule 不能为空")
            else:
                schedule_type = schedule_cfg.get("type", "")
                valid_types = {"daily", "weekly", "interval", "once", "cron"}
                if schedule_type not in valid_types:
                    errors.append(
                        f"{prefix}.schedule.type 无效: '{schedule_type}'，"
                        f"可选值: {', '.join(sorted(valid_types))}"
                    )
                # 按类型校验必要字段
                if schedule_type == "daily" and not schedule_cfg.get("time"):
                    errors.append(f"{prefix}.schedule 类型为 daily 时必须提供 'time' 字段")
                if schedule_type == "weekly":
                    if not schedule_cfg.get("time"):
                        errors.append(f"{prefix}.schedule 类型为 weekly 时必须提供 'time' 字段")
                    if not schedule_cfg.get("day"):
                        errors.append(f"{prefix}.schedule 类型为 weekly 时必须提供 'day' 字段")
                if schedule_type == "interval":
                    hours = schedule_cfg.get("hours", 0)
                    minutes = schedule_cfg.get("minutes", 0)
                    if hours == 0 and minutes == 0:
                        errors.append(f"{prefix}.schedule 类型为 interval 时必须设置 hours 或 minutes")
                if schedule_type == "once" and not schedule_cfg.get("datetime"):
                    errors.append(f"{prefix}.schedule 类型为 once 时必须提供 'datetime' 字段")
                if schedule_type == "cron" and not schedule_cfg.get("cron"):
                    errors.append(f"{prefix}.schedule 类型为 cron 时必须提供 'cron' 字段")
            # 校验 body_type
            body_type = email_cfg.get("body_type", "plain")
            if body_type not in ("plain", "html"):
                errors.append(f"{prefix}.body_type 无效: '{body_type}'，可选值: plain, html")

    # 3. 校验 settings 节（可选，给默认值即可）
    settings = config.get("settings", {})
    interval = settings.get("check_interval_seconds", 30)
    if not isinstance(interval, (int, float)) or interval <= 0:
        errors.append("settings.check_interval_seconds 必须为正数")

    return errors


# ===================== 邮件构建与发送 =====================

def _replace_placeholders(text: str) -> str:
    """替换文本中的占位符。

    支持的占位符:
      {date}      → 当前日期 (YYYY-MM-DD)
      {time}      → 当前时间 (HH:MM:SS)
      {datetime}  → 当前日期时间 (YYYY-MM-DD HH:MM:SS)
      {weekday}   → 当前星期 (星期一~星期日)
    """
    now = datetime.now()
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    replacements = {
        "{datetime}": now.strftime("%Y-%m-%d %H:%M:%S"),
        "{date}":     now.strftime("%Y-%m-%d"),
        "{time}":     now.strftime("%H:%M:%S"),
        "{weekday}":  weekday_names[now.weekday()],
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    return text


def _build_message(
    smtp_config: dict[str, Any],
    email_config: dict[str, Any],
) -> MIMEMultipart:
    """根据配置构建 MIMEMultipart 邮件对象。

    Args:
        smtp_config: SMTP 配置节
        email_config: 单个邮件任务配置节

    Returns:
        构建好的 MIMEMultipart 对象
    """
    # 替换占位符
    subject = _replace_placeholders(email_config.get("subject", ""))
    body = _replace_placeholders(email_config.get("body", ""))

    body_type = email_config.get("body_type", "plain")
    subtype = "html" if body_type == "html" else "plain"

    msg = MIMEMultipart()
    # 发件人：使用 formataddr 确保 RFC 5322/2047 合规（QQ 邮箱严格校验）
    sender_name = smtp_config.get("sender_name", "")
    if sender_name:
        msg["From"] = formataddr((sender_name, smtp_config["username"]))
    else:
        msg["From"] = smtp_config["username"]

    # 收件人（多个用逗号分隔）
    recipients = email_config.get("recipients", [])
    msg["To"] = ", ".join(recipients)

    # 抄送
    cc_list = email_config.get("cc", [])
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    # 主题
    msg["Subject"] = Header(subject, "utf-8")

    # 正文
    msg.attach(MIMEText(body, subtype, "utf-8"))

    return msg


def send_email(
    smtp_config: dict[str, Any],
    email_config: dict[str, Any],
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """发送单封邮件。

    Args:
        smtp_config: SMTP 配置节
        email_config: 单个邮件任务配置节
        dry_run: 若为 True，则只打印邮件信息而不实际发送

    Returns:
        (是否成功, 结果描述) 元组
    """
    task_name = email_config.get("name", "未命名任务")
    recipients = email_config.get("recipients", [])

    # 构建邮件
    msg = _build_message(smtp_config, email_config)
    all_recipients = recipients + email_config.get("cc", [])

    # 干运行模式：只打印信息
    if dry_run:
        subject = msg["Subject"]
        info = (
            f"[干运行] 任务: {task_name}\n"
            f"  发件人: {msg['From']}\n"
            f"  收件人: {', '.join(recipients)}\n"
            f"  抄送:   {', '.join(email_config.get('cc', [])) or '(无)'}\n"
            f"  主题:   {subject}"
        )
        logger.info(info)
        return True, "干运行模式，未实际发送"

    # 实际发送
    server: smtplib.SMTP | None = None
    try:
        smtp_server = smtp_config["server"]
        smtp_port = smtp_config["port"]
        use_tls = smtp_config.get("use_tls", True)

        # 建立连接
        if use_tls:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)

        # 登录
        server.login(smtp_config["username"], smtp_config["password"])

        # 发送
        server.sendmail(smtp_config["username"], all_recipients, msg.as_string())

        logger.info(f"[成功] 任务 '{task_name}' → {', '.join(recipients)}")
        return True, f"发送成功 → {', '.join(recipients)}"

    except smtplib.SMTPAuthenticationError as e:
        err = f"SMTP 认证失败，请检查用户名和密码/授权码: {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    except smtplib.SMTPConnectError as e:
        err = f"SMTP 连接失败 ({smtp_config['server']}:{smtp_config['port']}): {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    except smtplib.SMTPRecipientsRefused as e:
        err = f"收件人地址被拒绝: {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    except smtplib.SMTPServerDisconnected as e:
        err = f"SMTP 服务器意外断开连接: {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    except smtplib.SMTPException as e:
        err = f"SMTP 发送异常: {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    except OSError as e:
        err = f"网络错误: {e}"
        logger.error(f"[失败] 任务 '{task_name}': {err}")
        return False, err

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def send_email_with_retry(
    smtp_config: dict[str, Any],
    email_config: dict[str, Any],
    *,
    max_retries: int = 3,
    retry_delay: float = 60.0,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """带重试机制的邮件发送。

    Args:
        smtp_config: SMTP 配置节
        email_config: 单个邮件任务配置节
        max_retries: 最大重试次数（不含首次发送）
        retry_delay: 重试间隔（秒）
        dry_run: 是否为干运行模式

    Returns:
        (是否成功, 结果描述) 元组
    """
    task_name = email_config.get("name", "未命名任务")

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"任务 '{task_name}' 第 {attempt} 次重试，等待 {retry_delay:.0f} 秒...")
            time.sleep(retry_delay)

        success, msg = send_email(smtp_config, email_config, dry_run=dry_run)
        if success:
            return True, msg

        # 最后一次尝试失败不再等待
        if attempt < max_retries:
            logger.warning(f"任务 '{task_name}' 发送失败，将重试 ({attempt + 1}/{max_retries}): {msg}")

    return False, f"已达最大重试次数 ({max_retries})，发送失败"
