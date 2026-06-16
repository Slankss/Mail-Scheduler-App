from apscheduler.schedulers.background import BackgroundScheduler

import database
from mailer import send_email

JOB_ID = "mail_send_job"

scheduler = BackgroundScheduler()
scheduler.start()


def send_batch():
    settings = database.get_settings()
    if not settings or not settings["smtp_email"]:
        return

    batch_size = settings["batch_size"] or 1

    # Enforce the daily limit: never send more than `daily_limit` mails per day.
    # A value of 0 (or missing) means unlimited.
    daily_limit = settings["daily_limit"] or 0
    if daily_limit > 0:
        remaining = daily_limit - database.count_sent_today()
        if remaining <= 0:
            return  # gunluk limit doldu, bugun artik gondermeyiz
        batch_size = min(batch_size, remaining)

    attachments = [a["path"] for a in database.get_attachments()]

    # Retry failed contacts first, then send to pending ones.
    contacts = database.get_sendable_contacts(batch_size)
    for contact in contacts:
        try:
            send_email(
                settings,
                contact["email"],
                settings["subject"],
                settings["body"],
                attachments=attachments,
            )
            database.mark_contact_sent(contact["id"])
        except Exception:
            database.mark_contact_failed(contact["id"])


def start():
    settings = database.get_settings()
    interval = settings["interval_minutes"] or 5

    if scheduler.get_job(JOB_ID):
        scheduler.remove_job(JOB_ID)

    scheduler.add_job(send_batch, "interval", minutes=interval, id=JOB_ID)
    database.set_scheduler_active(True)


def stop():
    if scheduler.get_job(JOB_ID):
        scheduler.remove_job(JOB_ID)
    database.set_scheduler_active(False)


def is_running():
    return scheduler.get_job(JOB_ID) is not None
