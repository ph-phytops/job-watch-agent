"""Send the daily digest by email.

Uses the same Gmail account as the collector (SMTP over SSL); the recipient
comes from JOBWATCH_NOTIFY_TO in .env (defaults to the account itself).
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def send_digest(subject: str, body: str) -> None:
    user = os.environ["JOBWATCH_EMAIL_USER"]
    password = os.environ["JOBWATCH_EMAIL_PASSWORD"]
    recipient = os.environ.get("JOBWATCH_NOTIFY_TO", user)

    message = EmailMessage()
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)
