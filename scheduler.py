from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

import database
from mailer import send_email

JOB_ID = "mail_send_job"
SCHEDULE_JOB_PREFIX = "scheduled_start_"

# Uygulama kapaliyken kacirilan bir baslangic, bu sure icindeyse yine de
# calistirilir. Daha eskiyse "kacirildi" olarak isaretlenir: gunler once
# planlanmis bir gonderim, aciliste habersizce baslamamalidir.
MISFIRE_GRACE = timedelta(hours=1)

plascheduler = BackgroundScheduler()
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


def _job_id(schedule_id):
    return f"{SCHEDULE_JOB_PREFIX}{schedule_id}"


def _fire_schedule(schedule_id):
    """Zamani gelen baslangici calistirir (APScheduler tarafindan cagrilir)."""
    settings = database.get_settings()
    if not settings["smtp_email"] or not settings["smtp_password"]:
        database.set_schedule_status(
            schedule_id, "failed", "Mail ayarlari eksik oldugu icin baslatilamadi."
        )
        return
    start()
    database.set_schedule_status(schedule_id, "done")


def schedule_start(start_at: datetime):
    """Verilen zamanda otomatik gonderimi baslatacak bir is olusturur.

    Returns: (schedule_id, hata_mesaji). Gecmis bir zaman verilirse hata doner.
    """
    if start_at <= datetime.now():
        return None, "Baslangic zamani gelecekte olmali."

    schedule_id = database.add_schedule(start_at.isoformat(timespec="seconds"))
    scheduler.add_job(
        _fire_schedule, "date", run_date=start_at,
        args=[schedule_id], id=_job_id(schedule_id),
    )
    return schedule_id, None


def cancel_schedule(schedule_id):
    """Bekleyen bir zamanlamayi iptal eder."""
    job = scheduler.get_job(_job_id(schedule_id))
    if job:
        scheduler.remove_job(_job_id(schedule_id))
    return database.set_schedule_status(schedule_id, "cancelled") > 0


def restore_schedules():
    """Kayitli bekleyen zamanlamalari APScheduler'a geri yukler.

    Isler bellekte tutuldugu icin her acilista yeniden kurulmalidir. Uygulama
    kapaliyken zamani gecmis olanlar MISFIRE_GRACE icindeyse hemen calisir,
    daha eskiyse "kacirildi" olarak isaretlenir.
    """
    now = datetime.now()
    for row in database.get_pending_schedules():
        try:
            start_at = datetime.fromisoformat(row["start_at"])
        except (TypeError, ValueError):
            database.set_schedule_status(row["id"], "failed", "Gecersiz tarih.")
            continue

        if start_at > now:
            scheduler.add_job(
                _fire_schedule, "date", run_date=start_at,
                args=[row["id"]], id=_job_id(row["id"]), replace_existing=True,
            )
        elif now - start_at <= MISFIRE_GRACE:
            _fire_schedule(row["id"])
        else:
            database.set_schedule_status(
                row["id"], "missed", "Uygulama kapali oldugu icin calistirilamadi."
            )
