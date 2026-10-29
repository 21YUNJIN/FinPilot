"""Alert delivery channels.

Supported (all optional, all best-effort):

* ``macos``      - desktop notification via ``osascript``
* ``wecom``      - 企业微信群机器人 webhook
* ``serverchan`` - Server酱 (方糖) 微信推送
* ``email``      - SMTP email

Credentials live in ``~/.config/stockquant/notify.env`` (never committed):

    STOCKQUANT_NOTIFY_MACOS=1
    STOCKQUANT_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
    STOCKQUANT_SERVERCHAN_KEY=SCT...
    STOCKQUANT_SMTP_HOST=smtp.qq.com
    STOCKQUANT_SMTP_PORT=465
    STOCKQUANT_SMTP_USER=you@qq.com
    STOCKQUANT_SMTP_PASSWORD=授权码
    STOCKQUANT_SMTP_TO=you@qq.com
"""

from __future__ import annotations

import json
import smtplib
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_NOTIFY_ENV = "~/.config/stockquant/notify.env"

SEVERITY_LABEL = {"critical": "紧急", "warning": "注意", "opportunity": "机会"}
SEVERITY_ORDER = {"critical": 0, "warning": 1, "opportunity": 2}


@dataclass
class NotifyConfig:
    macos: bool = True
    wecom_webhook: str = ""
    serverchan_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_to: str = ""

    def channels(self) -> List[str]:
        available = []
        if self.macos:
            available.append("macos")
        if self.wecom_webhook:
            available.append("wecom")
        if self.serverchan_key:
            available.append("serverchan")
        if self.smtp_host and self.smtp_user and self.smtp_to:
            available.append("email")
        return available


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_notify_config(path: str = DEFAULT_NOTIFY_ENV) -> NotifyConfig:
    values: Dict[str, str] = {}
    env_path = Path(path).expanduser()
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    try:
        port = int(values.get("STOCKQUANT_SMTP_PORT", "465") or 465)
    except Exception:
        port = 465
    return NotifyConfig(
        macos=_truthy(values.get("STOCKQUANT_NOTIFY_MACOS", "1")),
        wecom_webhook=values.get("STOCKQUANT_WECOM_WEBHOOK", ""),
        serverchan_key=values.get("STOCKQUANT_SERVERCHAN_KEY", ""),
        smtp_host=values.get("STOCKQUANT_SMTP_HOST", ""),
        smtp_port=port,
        smtp_user=values.get("STOCKQUANT_SMTP_USER", ""),
        smtp_password=values.get("STOCKQUANT_SMTP_PASSWORD", ""),
        smtp_to=values.get("STOCKQUANT_SMTP_TO", ""),
    )


def sort_alerts(alerts: List[Dict[str, object]]) -> List[Dict[str, object]]:
    return sorted(alerts, key=lambda a: SEVERITY_ORDER.get(str(a.get("severity")), 3))


def format_alert_text(alerts: List[Dict[str, object]]) -> str:
    lines = []
    for alert in sort_alerts(alerts):
        label = SEVERITY_LABEL.get(str(alert.get("severity")), "")
        lines.append(f"[{label}] {alert.get('title')}")
        lines.append(f"    {alert.get('message')}")
    return "\n".join(lines)


def send_macos(title: str, message: str) -> bool:
    script = "display notification {} with title {}".format(
        json.dumps(message, ensure_ascii=False), json.dumps(title, ensure_ascii=False)
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def send_wecom(webhook: str, text: str, opener=urllib.request.urlopen) -> bool:
    payload = json.dumps({"msgtype": "text", "text": {"content": text}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = opener(req, timeout=10)
        body = resp.read().decode("utf-8", "ignore")
        return '"errcode":0' in body or '"errcode": 0' in body
    except Exception:
        return False


def send_serverchan(key: str, title: str, text: str, opener=urllib.request.urlopen) -> bool:
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title, "desp": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        resp = opener(req, timeout=10)
        body = resp.read().decode("utf-8", "ignore")
        return '"code":0' in body or '"code": 0' in body
    except Exception:
        return False


def send_email(cfg: NotifyConfig, subject: str, text: str) -> bool:
    if not (cfg.smtp_host and cfg.smtp_user and cfg.smtp_to):
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.smtp_to
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.starttls()
        with server:
            if cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.sendmail(cfg.smtp_user, [cfg.smtp_to], msg.as_string())
        return True
    except Exception:
        return False


def dispatch(alerts: List[Dict[str, object]], cfg: Optional[NotifyConfig] = None) -> Dict[str, bool]:
    """Fan out alerts to every configured channel. Returns per-channel status."""
    if not alerts:
        return {}
    cfg = cfg or load_notify_config()
    text = format_alert_text(alerts)
    top = alerts[0] if alerts else {}
    title = "StockQuant 盯盘提醒"
    if len(alerts) == 1 and top.get("title"):
        title = f"StockQuant · {top.get('title')}"
    elif alerts:
        title = f"StockQuant · {len(alerts)} 条盯盘提醒"

    results: Dict[str, bool] = {}
    if cfg.macos:
        results["macos"] = send_macos(title, text)
    if cfg.wecom_webhook:
        results["wecom"] = send_wecom(cfg.wecom_webhook, f"{title}\n\n{text}")
    if cfg.serverchan_key:
        results["serverchan"] = send_serverchan(cfg.serverchan_key, title, text)
    if cfg.smtp_host and cfg.smtp_user and cfg.smtp_to:
        results["email"] = send_email(cfg, title, text)
    return results
