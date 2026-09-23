"""Alert delivery: log, Slack incoming webhook, email (SMTP).

A failing channel is logged and skipped; alerts never crash a trading run.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import requests

from .config import AlertConfig

log = logging.getLogger(__name__)


@dataclass
class Alert:
    kind: str       # trade | rank_change | high_confidence | drawdown | error | info
    title: str
    body: str = ""


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class LogChannel:
    name = "log"

    def send(self, alert: Alert) -> None:
        log.info("ALERT [%s] %s %s", alert.kind, alert.title, f"- {alert.body}" if alert.body else "")


class SlackChannel:
    name = "slack"
    ICONS = {"trade": ":chart_with_upwards_trend:", "rank_change": ":trophy:",
             "high_confidence": ":bulb:", "drawdown": ":warning:", "error": ":x:"}

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, alert: Alert) -> None:
        icon = self.ICONS.get(alert.kind, ":information_source:")
        text = f"{icon} *{alert.title}*" + (f"\n{alert.body}" if alert.body else "")
        resp = requests.post(self.webhook_url, json={"text": text}, timeout=self.timeout)
        resp.raise_for_status()


class EmailChannel:
    name = "email"

    def __init__(self, cfg: AlertConfig):
        self.cfg = cfg

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[SMG bot] {alert.title}"
        msg["From"] = self.cfg.email_from or self.cfg.smtp_username
        msg["To"] = self.cfg.email_to
        msg.set_content(alert.body or alert.title)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=20) as smtp:
            if self.cfg.smtp_use_tls:
                smtp.starttls()
            if self.cfg.smtp_username:
                smtp.login(self.cfg.smtp_username, self.cfg.smtp_password)
            smtp.send_message(msg)


class AlertManager:
    def __init__(self, channels: list[AlertChannel] | None = None):
        self.channels = channels if channels is not None else [LogChannel()]
        self.sent: list[Alert] = []

    def send(self, kind: str, title: str, body: str = "") -> None:
        alert = Alert(kind, title, body)
        self.sent.append(alert)
        for ch in self.channels:
            try:
                ch.send(alert)
            except Exception as exc:
                log.error("alert channel %s failed: %s", ch.name, exc)


def build_alert_manager(cfg: AlertConfig) -> AlertManager:
    channels: list[AlertChannel] = [LogChannel()]
    if cfg.slack_webhook_url:
        channels.append(SlackChannel(cfg.slack_webhook_url))
    if cfg.smtp_host and cfg.email_to:
        channels.append(EmailChannel(cfg))
    return AlertManager(channels)
