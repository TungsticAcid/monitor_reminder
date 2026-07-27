#!/usr/bin/env python
"""
计算任务完成检测器（邮件通知版）
=================================
通过 SSH 监控远程服务器的计算进程 CPU 占用率，计算完成时自动发送邮件通知。

检测逻辑：
  1. 同时监控多台服务器
  2. 每台服务器 SSH → ps aux 汇总目标进程的 %CPU
  3. CPU > 阈值 → 该服务器还在算；CPU ≤ 阈值 → 确认 log 是否写完
  4. 某台服务器 CPU 降至阈值以下且 log 确认完成 → 发送邮件通知
  5. 确认后该服务器不再提醒，直到 CPU 再次超过阈值（有新作业）

用法：
    python monitor_reminder.py                      # 按配置间隔检查（默认）
    python monitor_reminder.py --interval 120       # 每 2 分钟检查
    python monitor_reminder.py --once               # 只检查一次
    python monitor_reminder.py --dry-run            # 测试邮件配置（发一封测试邮件后退出）

依赖：pip install paramiko
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from getpass import getpass
from pathlib import Path
from typing import Any

# 修复 Windows 终端 GBK 编码下 emoji 显示问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import paramiko
except ImportError:
    print("请先安装 paramiko：conda install -c conda-forge paramiko")
    sys.exit(1)

# 邮件发送模块（从 12/ 复用，已验证 QQ 邮箱 SMTP 可用）
from email_utils import send_email

# ===================== 日志 =====================
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("monitor_reminder")

# 抑制 paramiko 内部日志
logging.getLogger("paramiko").setLevel(logging.WARNING)


# ===================== 监控配置（从 config.json 加载） =====================
# 以下变量在 load_config() 中从 JSON 文件填充
SERVER_POOL: dict[int, dict[str, Any]] = {}
MONITOR: list[int] = []
CHECK_INTERVAL: int = 200
CPU_THRESHOLD: float = 200.0
CHECK_TIMEOUT: int = 60


def load_config(config_path: str = "config.json") -> dict[str, Any]:
    """加载配置文件（SMTP + 告警 + 监控参数 + 服务器列表）。

    读取 config.json 并填充模块级变量：
      SERVER_POOL, MONITOR, CHECK_INTERVAL, CPU_THRESHOLD, CHECK_TIMEOUT

    Args:
        config_path: 配置文件路径

    Returns:
        完整配置字典 {"smtp": ..., "alert": ..., "monitor": ...}
    """
    global SERVER_POOL, MONITOR, CHECK_INTERVAL, CPU_THRESHOLD, CHECK_TIMEOUT

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path.absolute()}")

    with open(path, "r", encoding="utf-8") as f:
        config: dict[str, Any] = json.load(f)

    # 校验 smtp
    smtp = config.get("smtp")
    if not smtp:
        raise ValueError("配置缺少 'smtp' 节")
    for key in ("server", "port", "username", "password"):
        if not smtp.get(key):
            raise ValueError(f"smtp.{key} 不能为空")

    # 校验 alert
    alert = config.get("alert")
    if not alert:
        raise ValueError("配置缺少 'alert' 节")
    if not alert.get("recipients"):
        raise ValueError("alert.recipients 不能为空，至少需要一个收件人")

    # 读取 monitor 节 → 填充模块级变量
    monitor = config.get("monitor")
    if not monitor:
        raise ValueError("配置缺少 'monitor' 节")
    if not monitor.get("servers"):
        raise ValueError("monitor.servers 不能为空")
    if not monitor.get("monitor_list"):
        raise ValueError("monitor.monitor_list 不能为空")

    SERVER_POOL = monitor["servers"]
    MONITOR = monitor["monitor_list"]
    CHECK_INTERVAL = monitor.get("check_interval", 200)
    CPU_THRESHOLD = monitor.get("cpu_threshold", 200.0)
    CHECK_TIMEOUT = monitor.get("check_timeout", 60)

    # 校验 monitor_list 中的每个 key 在 servers 中都存在
    for name in MONITOR:
        if name not in SERVER_POOL:
            raise ValueError(f"monitor.monitor_list 中的 '{name}' 在 monitor.servers 中不存在")

    logger.info(f"配置加载成功: {path.absolute()}")
    logger.info(f"  监控 {len(MONITOR)} 台服务器: {MONITOR}")
    logger.info(f"  检查间隔: {CHECK_INTERVAL}s, CPU 阈值: {CPU_THRESHOLD}%")
    logger.info(f"  告警收件人: {', '.join(alert['recipients'])}")
    return config


# ===================== 原 load_email_config 保留为别名 =====================
load_email_config = load_config


def send_alert_email(
    smtp_config: dict[str, Any],
    alert_config: dict[str, Any],
    server_name: str,
    info: dict[str, Any],
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """发送计算完成邮件通知。

    Args:
        smtp_config:  SMTP 配置
        alert_config: 告警配置（收件人、模板等）
        server_name:  服务器名称（如 195, 87）
        info:          check_logs() 返回的完成信息
                       {"total": N, "normal": N, "error": N, "time": "..."}
        dry_run:      是否为干运行模式

    Returns:
        (是否成功, 结果描述)
    """
    total = info.get("total", "?")
    normal = info.get("normal", "?")
    error = info.get("error", "?")
    complete_time = info.get("time", "未知")

    # 模板变量替换
    template_vars = {
        "name": str(server_name),
        "total": str(total),
        "normal": str(normal),
        "error": str(error),
        "time": complete_time,
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }

    subject_template = alert_config.get("subject_template", "计算完成 - 服务器 {name}")
    body_template = alert_config.get(
        "body_template",
        "服务器 {name} 计算完成！\n正常: {normal}/{total}\n报错: {error}/{total}",
    )

    subject = subject_template
    body = body_template
    for key, val in template_vars.items():
        subject = subject.replace("{" + key + "}", val)
        body = body.replace("{" + key + "}", val)

    email_config: dict[str, Any] = {
        "name": f"监控提醒-服务器{server_name}",
        "subject": subject,
        "body": body,
        "body_type": alert_config.get("body_type", "plain"),
        "recipients": alert_config["recipients"],
        "cc": alert_config.get("cc", []),
    }

    return send_email(smtp_config, email_config, dry_run=dry_run)


def send_test_email(
    smtp_config: dict[str, Any],
    alert_config: dict[str, Any],
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """发送测试邮件，验证邮件配置是否正确。

    Args:
        smtp_config:  SMTP 配置
        alert_config: 告警配置
        dry_run:      是否为干运行模式

    Returns:
        (是否成功, 结果描述)
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    test_info = {
        "total": "0",
        "normal": "0",
        "error": "0",
        "time": now,
    }
    # 临时覆盖主题以标识这是测试邮件
    original_subject = alert_config.get("subject_template", "")
    alert_config = dict(alert_config)
    alert_config["subject_template"] = "【测试】" + original_subject

    return send_alert_email(
        smtp_config, alert_config, "TEST", test_info, dry_run=dry_run,
    )


# 每台服务器的 SSH 连接（key = server name）
_clients: dict[str, paramiko.SSHClient] = {}
_sshpass_warned: set[str] = set()  # 避免重复提示 sshpass 未安装


def _resolve(name: int | str) -> dict:
    """从 SERVER_POOL 取出配置，注入 name 字段，返回合并后的 dict。"""
    cfg = SERVER_POOL[name].copy()
    cfg["name"] = name
    return cfg


def ssh_connect(svr: dict, silent: bool = False) -> bool:
    """为指定服务器建立 SSH 连接。silent=True 时不打印错误信息。"""
    name = svr["name"]
    password = svr["pass"]
    if password is None:
        password = getpass(f"请输入 {svr['user']}@{svr['host']} 密码: ")

    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            hostname=svr["host"], port=svr["port"],
            username=svr["user"], password=password,
            timeout=10, allow_agent=False, look_for_keys=False,
            banner_timeout=15, auth_timeout=15,
        )
        _clients[name] = c
        return True
    except paramiko.AuthenticationException:
        if not silent:
            print(f"  [{name}] ❌ 密码错误")
        return False
    except Exception as e:
        if not silent:
            print(f"  [{name}] ❌ 连接失败: {e}")
        return False


def ssh_exec(svr: dict, command: str) -> str | None:
    """在指定服务器上执行命令，返回 stdout。paramiko 失败则回退到系统 ssh。"""
    name = svr["name"]
    # ── 方式 1: paramiko ──
    for attempt in range(2):
        c = _clients.get(name)
        if c is None or not c.get_transport() or not c.get_transport().is_active():
            if not ssh_connect(svr, silent=True):
                break  # paramiko 连不上，跳出试 fallback
            c = _clients.get(name)
        try:
            _, stdout, stderr = c.exec_command(command, timeout=CHECK_TIMEOUT)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return out if out else (err if err else "")
        except Exception:
            _clients.pop(name, None)
            if attempt == 0:
                time.sleep(2)
            continue

    # ── 方式 2: 系统 ssh 回退（需要 sshpass 或密钥） ──
    host, port = svr["host"], svr["port"]
    user, password = svr["user"], svr.get("pass")
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    try:
        if password:
            result = subprocess.run(
                ["sshpass", "-p", password, "ssh"] + ssh_opts +
                ["-p", str(port), f"{user}@{host}", command],
                capture_output=True, text=True, timeout=CHECK_TIMEOUT,
            )
        else:
            result = subprocess.run(
                ["ssh"] + ssh_opts + ["-o", "BatchMode=yes",
                "-p", str(port), f"{user}@{host}", command],
                capture_output=True, text=True, timeout=CHECK_TIMEOUT,
            )
        return result.stdout.strip() or None
    except FileNotFoundError:
        if password and name not in _sshpass_warned:
            print(f"  [{name}] sshpass 未安装，paramiko 也连不上，请:")
            print(f"         choco install sshpass   （管理员终端运行）")
            print(f"         或对该服务器设置 SSH 密钥后把 pass 改为 None")
            _sshpass_warned.add(name)
        return None
    except Exception:
        return None


def check_cpu(svr: dict) -> dict | None:
    """ps aux 汇总目标进程 %CPU 及 COMMAND，只匹 $11（命令列）不匹 $1（用户名）。"""
    procs = "|".join(svr.get("procs", ["g09", "g16"]))
    # $11 是 ps aux 的 COMMAND 列第一词; ~ 包含匹配即可，不会误伤用户名
    cmd = (
        "ps aux --no-headers 2>/dev/null | awk '"
        f"$11~/^({procs})$|{procs}\\.exe/"
        "{cpu+=$3; mem+=$4; n++; sub(/.*\\//,\"\",$11); cmds[n]=$11}"
        "END{printf \"%.1f %.1f %d\", cpu, mem, n;"
        "for(i=1;i<=n;i++) printf \" %s\", cmds[i]}'"
    )
    output = ssh_exec(svr, cmd)
    if not output:
        return None
    try:
        parts = output.split()
        cpu_pct = float(parts[0])
        mem_pct = float(parts[1]) if len(parts) >= 2 else 0.0
        count = int(parts[2]) if len(parts) >= 3 else 0
        cmds = parts[3:] if len(parts) > 3 else []
        return {"cpu_pct": cpu_pct, "mem_pct": mem_pct, "proc_count": count, "commands": cmds}
    except (ValueError, IndexError):
        return None


def check_logs(svr: dict) -> dict | None:
    """检查 marker 文件或直接 grep log。"""
    d = svr["dir"]
    # 先查标记文件
    out = ssh_exec(svr, f"cat {d}/CALC_COMPLETE.txt 2>/dev/null || echo 'NOT_DONE'")
    if out and out != "NOT_DONE":
        info: dict = {}
        for line in out.split("\n"):
            line = line.strip()
            if line.startswith("总任务数:"):   info["total"] = int(line.split(":")[1])
            elif line.startswith("正常终止:"): info["normal"] = int(line.split(":")[1])
            elif line.startswith("报错终止:"): info["error"] = int(line.split(":")[1])
            elif line.startswith("完成时间:"): info["time"] = line.split(":", 1)[1].strip()
        if info:
            return info
    # 再直接 grep
    out = ssh_exec(svr,
        f"cd {d} 2>/dev/null && "
        "total=$(ls *.gjf 2>/dev/null | wc -l); "
        "normal=$(grep -l 'Normal termination' *.log 2>/dev/null | wc -l); "
        "error=$(grep -l 'Error termination' *.log 2>/dev/null | wc -l); "
        'echo "${total} ${normal} ${error}"'
    )
    if out:
        try:
            t, n, e = (int(x) for x in out.split())
            if t > 0 and (n + e) >= t:
                return {"total": t, "normal": n, "error": e, "time": ""}
        except (ValueError, IndexError):
            pass
    return None


def cpu_bar(cpu_pct: float, nproc: int, width: int = 24) -> str:
    """CPU 进度条，满值 = 总核数 × 100%。"""
    total: float = float(nproc) * 100.0       # 例如 48 核 = 4800%
    ratio: float = min(cpu_pct / total, 1.0) if total > 0 else 0.0
    filled: int = int(width * ratio)
    if ratio > 0.8:       ch = "█"
    elif ratio > 0.5:     ch = "▓"
    elif ratio > 0.01:    ch = "▒"
    else:                 ch = " "
    return f"[{ch * filled + '░' * (width - filled)}]"


def try_connect_all() -> int:
    """尝试连接所有未连接的服务器，返回已连接数。"""
    connected = 0
    for name in MONITOR:
        svr = _resolve(name)
        c = _clients.get(name)
        if c is not None and c.get_transport() and c.get_transport().is_active():
            connected += 1
            continue
        print(f"[{time.strftime('%H:%M:%S')}] 连接 {name} ({svr['host']})...")
        if ssh_connect(svr):
            connected += 1
        # 失败不阻塞，本轮跳过，下轮自动重试
    return connected


# ===================== 主函数 =====================

def main() -> None:
    global CHECK_INTERVAL
    interval = CHECK_INTERVAL
    once = False
    dry_run = False
    config_path = "config.json"

    # ── 解析命令行参数 ──
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--interval", "-i") and i + 1 < len(args):
            interval = int(args[i + 1]); i += 2
        elif args[i] == "--once":
            once = True; i += 1
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        elif args[i] in ("--config", "-c") and i + 1 < len(args):
            config_path = args[i + 1]; i += 2
        else:
            i += 1

    # ── 加载配置（SMTP + 告警 + 监控参数） ──
    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f"错误: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"配置错误: {e}")
        sys.exit(1)

    smtp_config = config["smtp"]
    alert_config = config["alert"]

    # ── 测试邮件模式：发送一封测试邮件验证配置，然后退出 ──
    if dry_run:
        print("=" * 55)
        print("  计算监控 — 邮件配置测试")
        print("=" * 55)
        print("  📧 正在发送测试邮件...")
        print()
        # 测试邮件实际发送（不模拟），以验证 SMTP 配置是否可用
        success, msg = send_test_email(smtp_config, alert_config, dry_run=False)
        if success:
            print(f"\n✅ 邮件配置测试通过: {msg}")
        else:
            print(f"\n❌ 邮件配置测试失败: {msg}")
            sys.exit(1)
        return

    # ── 启动信息 ──
    print("=" * 55)
    print("  计算任务完成检测器（邮件通知版）")
    print("=" * 55)
    print(f"  监控 {len(MONITOR)} 台服务器:")
    for name in MONITOR:
        svr = _resolve(name)
        print(f"    - {name}: {svr['user']}@{svr['host']}:{svr['port']}  [{svr['dir']}]")
    print(f"  检查间隔: {interval}s（{interval // 60} 分钟）")
    print(f"  CPU 阈值: {CPU_THRESHOLD:.0f}%")
    print(f"  告警收件人: {', '.join(alert_config['recipients'])}")
    print()

    # ── 首次连接 ── 至少连上一台才能继续
    connected = try_connect_all()
    if connected == 0:
        print("  ❌ 所有服务器均无法连接，退出")
        sys.exit(1)
    print(f"  ✅ 已连接 {connected}/{len(MONITOR)} 台服务器\n")

    # ── 主循环 ──
    # server_state: 记录每台服务器上次"确认完成"的状态
    #   {"done": True, "info": {...}}  表示该服务器已完成并被确认过
    # notified_servers: 已在本次"完成周期"内发邮件提醒过的服务器集合
    #   当某台服务器 CPU 再次超过阈值时，自动移出该集合（重置提醒状态）
    # saw_running: 至少一次观察到 CPU > 阈值的服务器集合
    #   只有先被观察到"在跑"然后降下来，才触发完成提醒（启动时已空闲的不提醒）
    server_state: dict[str, dict] = {}
    notified_servers: set[str] = set()
    saw_running: set[str] = set()

    while True:
        timestamp = time.strftime("%H:%M:%S")

        lines: list[str] = []

        for name in MONITOR:
            svr = _resolve(name)
            cpu = check_cpu(svr)
            if cpu is None:
                lines.append(f"  [{name}] ⚠️  查询失败（下轮自动重试）")
                continue

            thresh = svr.get("thresh") or CPU_THRESHOLD
            nproc = svr.get("nproc")
            if nproc is None:
                out = ssh_exec(svr, "nproc 2>/dev/null || echo 48")
                try:
                    nproc = int(out.strip()) if out else 48
                except (ValueError, AttributeError):
                    nproc = 48
            bar = cpu_bar(cpu["cpu_pct"], nproc)
            running = cpu["cpu_pct"] > thresh

            if running:
                # ── 还在跑 → 标记"已观察到运行"，重置完成状态和通知标记 ──
                saw_running.add(name)
                if name in notified_servers:
                    notified_servers.discard(name)
                if name in server_state:
                    server_state.pop(name)

                procs_str = ", ".join(cpu["commands"][:4])
                if len(cpu["commands"]) > 4:
                    procs_str += f" +{len(cpu['commands']) - 4}"
                lines.append(
                    f"  [{name}] [{bar}] CPU {cpu['cpu_pct']:.0f}% | "
                    f"{procs_str} | 内存{cpu['mem_pct']:.0f}% | ⏳"
                )

            elif name in notified_servers:
                # ── 已完成且已通知过，不再提醒 ──
                info = server_state.get(name, {}).get("info", {})
                normal = info.get("normal", "?")
                total = info.get("total", "?")
                error = info.get("error", "?")
                mark = "✅" if error == 0 else "⚠️"
                lines.append(
                    f"  [{name}] [{bar}] {mark} 已通知 "
                    f"({normal}/{total} 正常)"
                )

            elif name not in server_state or not server_state[name].get("done"):
                # ── CPU ≤ 阈值且未确认过 ──
                if name not in saw_running:
                    # 从未被观察到"在跑" → 不查 log，直接显示空闲
                    lines.append(
                        f"  [{name}] [{bar}] CPU {cpu['cpu_pct']:.0f}% | 🚫 无任务"
                    )
                else:
                    # 之前看到过"在跑" → 检查 log 是否完成
                    info = check_logs(svr)
                    # 当 check_logs 无结果但进程已全部退出时，生成兜底信息
                    if info is None and cpu["proc_count"] == 0:
                        info = {
                            "total": "?",
                            "normal": "?",
                            "error": "?",
                            "time": "进程已全部停止（log 中未找到完成标记）",
                        }

                    if info:
                        server_state[name] = {"done": True, "info": info}
                        total = info.get("total", "?")
                        normal = info.get("normal", "?")
                        error = info.get("error", "?")
                        mark = "✅" if (error == 0 or error == "?") else "⚠️"
                        lines.append(
                            f"  [{name}] [{bar}] {mark} 完成 "
                            f"({normal}/{total} 正常)"
                        )
                        # ── 发送邮件通知 ──
                        success, msg = send_alert_email(
                            smtp_config, alert_config, name, info,
                        )
                        if success:
                            print(f"  📧 已发送邮件提醒 [{name}] → {', '.join(alert_config['recipients'])}")
                        else:
                            print(f"  ⚠️ 邮件发送失败 [{name}]: {msg}")
                        notified_servers.add(name)
                    else:
                        lines.append(
                            f"  [{name}] [{bar}] CPU {cpu['cpu_pct']:.0f}% | 🔍 确认中..."
                        )
            else:
                # ── 已确认完成（但还没通知过的情况不会走到这里） ──
                info = server_state[name].get("info", {})
                normal = info.get("normal", "?")
                total = info.get("total", "?")
                error = info.get("error", "?")
                mark = "✅" if error == 0 else "⚠️"
                lines.append(
                    f"  [{name}] [{bar}] {mark} 已通知 "
                    f"({normal}/{total} 正常)"
                )

        # ── 输出 ──
        print(f"[{timestamp}]")
        for line in lines:
            print(line)

        if once:
            break
        print()
        time.sleep(interval)

    for c in _clients.values():
        c.close()


if __name__ == "__main__":
    main()
