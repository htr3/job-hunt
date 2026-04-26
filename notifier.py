"""Run-complete notifier.

Supports five channels, each independently toggled in
`config.notifications.<channel>`:

- **desktop** - via `plyer` (optional import; no-op on headless boxes)
- **email**   - SMTP over TLS using env vars SMTP_HOST/PORT/USER/PASSWORD/FROM/TO
- **telegram** - Bot API with TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
- **slack**   - Incoming webhook via SLACK_WEBHOOK_URL
- **whatsapp** - best-effort via CallMeBot's free API (`WHATSAPP_PHONE` + `WHATSAPP_APIKEY`)

No channel can crash the pipeline - every send is try/except-guarded and logs a
single warning on failure. Empty credentials silently skip the channel.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any

import requests

logger = logging.getLogger("notifier")


class Notifier:
    def __init__(self, config: dict[str, Any]) -> None:
        notif = config.get("notifications", {}) or {}
        self.desktop_on = bool(notif.get("desktop"))
        self.email_on = bool(notif.get("email"))
        self.telegram_on = bool(notif.get("telegram"))
        self.slack_on = bool(notif.get("slack"))
        self.whatsapp_on = bool(notif.get("whatsapp"))
        self._config = config

    def send_run_summary(self, summary: dict[str, Any], top_jobs: list | None = None) -> dict[str, bool]:
        """Fan-out a run summary to every enabled channel. Returns {channel: sent}."""
        title = "Job Hunt Agent - run complete"
        body = self._format_body(summary, top_jobs or [])
        results = {
            "desktop": self._send_desktop(title, body) if self.desktop_on else False,
            "email": self._send_email(title, body) if self.email_on else False,
            "telegram": self._send_telegram(body) if self.telegram_on else False,
            "slack": self._send_slack(body) if self.slack_on else False,
            "whatsapp": self._send_whatsapp(body) if self.whatsapp_on else False,
        }
        sent = [k for k, v in results.items() if v]
        skipped = [k for k, v in results.items() if v is False and getattr(self, f"{k}_on")]
        if sent:
            logger.info("Notifications sent: %s", ", ".join(sent))
        if skipped:
            logger.info("Notifications skipped (missing creds or error): %s", ", ".join(skipped))
        return results

    # ------------------------------------------------------------------ body
    @staticmethod
    def _format_body(summary: dict[str, Any], top_jobs: list) -> str:
        lines = [
            f"Platforms: {', '.join(summary.get('platforms') or [])}",
            f"Scraped:  {summary.get('total_scraped', 0)} ({summary.get('new_jobs', 0)} new)",
            f"Filtered: {summary.get('final_jobs', 0)}",
            f"Duration: {summary.get('duration_seconds', 0):.1f}s",
        ]
        if top_jobs:
            lines.append("")
            lines.append("Top matches:")
            for j in top_jobs[:5]:
                score = float(getattr(j, "match_score", 0) or 0)
                lines.append(
                    f"  [{score:>3.0f}] {getattr(j, 'title', '')[:60]} - {getattr(j, 'company', '')}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------ desktop
    def _send_desktop(self, title: str, body: str) -> bool:
        try:
            from plyer import notification  # type: ignore
        except Exception as e:
            logger.debug("plyer not available: %s", e)
            return False
        try:
            notification.notify(
                title=title,
                message=body[:256],  # plyer truncates anyway
                app_name="Job Hunt Agent",
                timeout=8,
            )
            return True
        except Exception as e:
            logger.warning("Desktop notification failed: %s", e)
            return False

    # ------------------------------------------------------------------ email
    def _send_email(self, subject: str, body: str) -> bool:
        host = os.environ.get("SMTP_HOST")
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        sender = os.environ.get("SMTP_FROM") or user
        to = os.environ.get("SMTP_TO") or user
        if not (host and user and password and to):
            logger.debug("Email skipped - set SMTP_HOST/USER/PASSWORD/TO in .env")
            return False
        msg = EmailMessage()
        msg["From"] = sender or ""
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls()
                    s.ehlo()
                except smtplib.SMTPException:
                    pass  # server doesn't advertise STARTTLS
                s.login(user, password)
                s.send_message(msg)
            return True
        except Exception as e:
            logger.warning("Email send failed: %s", e)
            return False

    # ------------------------------------------------------------------ telegram
    def _send_telegram(self, body: str) -> bool:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat_id):
            logger.debug("Telegram skipped - set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            r = requests.post(
                url,
                data={"chat_id": chat_id, "text": body, "disable_web_page_preview": "true"},
                timeout=15,
            )
            if not r.ok:
                logger.warning("Telegram API returned %s: %s", r.status_code, r.text[:200])
            return r.ok
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    # ------------------------------------------------------------------ slack
    def _send_slack(self, body: str) -> bool:
        webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook:
            logger.debug("Slack skipped - set SLACK_WEBHOOK_URL in .env")
            return False
        try:
            r = requests.post(
                webhook,
                json={"text": f"```\n{body}\n```"},
                timeout=15,
            )
            if not r.ok:
                logger.warning("Slack webhook returned %s: %s", r.status_code, r.text[:200])
            return r.ok
        except Exception as e:
            logger.warning("Slack send failed: %s", e)
            return False

    # ------------------------------------------------------------------ whatsapp
    def _send_whatsapp(self, body: str) -> bool:
        """Best-effort via CallMeBot (free tier). Requires WHATSAPP_PHONE + WHATSAPP_APIKEY."""
        phone = os.environ.get("WHATSAPP_PHONE")
        apikey = os.environ.get("WHATSAPP_APIKEY")
        if not (phone and apikey):
            logger.debug("WhatsApp skipped - set WHATSAPP_PHONE and WHATSAPP_APIKEY in .env")
            return False
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone, "text": body, "apikey": apikey},
                timeout=15,
            )
            if not r.ok:
                logger.warning("WhatsApp API returned %s: %s", r.status_code, r.text[:200])
            return r.ok
        except Exception as e:
            logger.warning("WhatsApp send failed: %s", e)
            return False


def send_all_notifications(
    config: dict[str, Any],
    summary: dict[str, Any],
    top_jobs: list | None = None,
) -> dict[str, bool]:
    """Functional wrapper used by `job_hunter.run_agent`."""
    return Notifier(config).send_run_summary(summary, top_jobs)
