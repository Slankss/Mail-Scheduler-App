import os
import smtplib
from email.message import EmailMessage
from mimetypes import guess_type


def connect(server_addr, port, email, password, timeout=30):
    """Giris yapilmis bir SMTP baglantisi dondurur.

    Port 465 ise SMTP_SSL, degilse STARTTLS kullanilir. Baglanti `with` ile
    kullanilmalidir; cikista QUIT gonderilir.
    """
    port = int(port)
    if port == 465:
        server = smtplib.SMTP_SSL(server_addr, port, timeout=timeout)
    else:
        server = smtplib.SMTP(server_addr, port, timeout=timeout)
        server.starttls()
    server.login(email, password)
    return server


def test_connection(server_addr, port, email, password, timeout=15):
    """Mail gondermeden sunucuya baglanip giris yapmayi dener.

    Basarisizsa smtplib/socket istisnasi firlatir.
    """
    with connect(server_addr, port, email, password, timeout=timeout) as server:
        server.noop()


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

    with connect(
        settings["smtp_server"],
        settings["smtp_port"],
        settings["smtp_email"],
        settings["smtp_password"],
    ) as server:
        server.send_message(msg)
