"""Email (SMTP) client — REFERENCE STUB (Layer A).

Sends the regression report. This stub logs instead of sending so the host runs
with no SMTP configured; wire up the real send where marked. A minimal, correct
smtplib implementation is included but GUARDED behind configured SMTP settings —
with none set (the default), it stays a no-op.
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from ..config import Settings
from ..utils.logging import get_logger

_log = get_logger("email")


class EmailClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def send_report(self, subject: str, body: str, html: bool = False) -> None:
        """Email the report to REPORT_TO. No-op (logs only) unless SMTP_HOST is
        set, so the host runs cleanly out of the box. The real path uses stdlib
        smtplib; swap in your provider's API here if you prefer."""
        if not self.s.smtp_host:
            _log.info("[STUB] SMTP not configured — would email %r to %s (not sent)",
                      subject, self.s.report_to)
            return
        msg = MIMEText(body, "html" if html else "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.s.report_from
        msg["To"] = self.s.report_to
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port) as smtp:
            smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
        _log.info("sent report %r to %s", subject, self.s.report_to)
