import os
import smtplib
from email.message import EmailMessage
from mimetypes import guess_type


def send_email(settings, to_email, subject, body, attachments=None):
    """Send a single email using SMTP settings stored in the settings row.

    `attachments` is an optional list of file paths to attach.
    Uses SMTP_SSL when port is 465, otherwise STARTTLS.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["smtp_email"]
    msg["To"] = to_email
    msg.set_content(body or "", subtype="plain", charset="utf-8")

    for path in attachments or []:
        if not path or not os.path.isfile(path):
            continue
        ctype, _ = guess_type(path)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(path),
            )

    server_addr = settings["smtp_server"]
    port = int(settings["smtp_port"])

    if port == 465:
        with smtplib.SMTP_SSL(server_addr, port, timeout=30) as server:
            server.login(settings["smtp_email"], settings["smtp_password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(server_addr, port, timeout=30) as server:
            server.starttls()
            server.login(settings["smtp_email"], settings["smtp_password"])
            server.send_message(msg)
