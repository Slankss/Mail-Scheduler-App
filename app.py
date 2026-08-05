import math
import os
import smtplib
from datetime import datetime, timedelta

import pandas as pd
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import database
import gmail_client
import mailer
import scheduler as sched
import verifier

UPLOAD_DIR = database.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = "mail-scheduler-secret-key"

database.init_db()
# APScheduler isleri bellekte durur; kayitli zamanlamalar her acilista kurulur.
sched.restore_schedules()


def compute_progress():
    """Build a progress snapshot for the dashboard / polling endpoint."""
    # Disabled companies never get a mail, so they are excluded here; otherwise
    # the bar could never reach 100%.
    stats = database.get_stats(only_enabled=True)
    settings = database.get_settings()
    running = sched.is_running()

    total = stats["total"]
    sent = stats["sent"]
    failed = stats["failed"]
    pending = stats["pending"]
    processed = sent + failed

    percent = round(processed / total * 100) if total else 0

    companies = [c for c in database.get_companies() if c["enabled"]]
    companies_total = len(companies)
    # A company counts as "sent" once all of its mails have gone out
    # (no pending and no failed left).
    companies_sent = sum(
        1 for c in companies if c["total"] > 0 and c["pending"] == 0 and c["failed"] == 0
    )

    interval = settings["interval_minutes"] or 5
    batch = settings["batch_size"] or 1

    started_at = settings["scheduler_started_at"] if running else None
    # Gunluk limit dolunca gonderim kendiliginden durur; kalan hak ve durma
    # nedeni gosterilir ki kullanici neden durdugunu anlasin.
    daily_limit = settings["daily_limit"] or 0
    sent_today = database.count_sent_today()
    stop_reason = None if running else settings["stop_reason"]

    # Estimated finish: number of remaining batches * interval.
    eta = None
    if pending > 0:
        remaining_batches = math.ceil(pending / batch)
        # While running the next batch fires within `interval` minutes; the
        # last batch finishes after remaining_batches intervals from now.
        eta = (datetime.now() + timedelta(minutes=remaining_batches * interval)).isoformat(
            timespec="seconds"
        )

    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "pending": pending,
        "disabled": stats["disabled"],
        "processed": processed,
        "percent": percent,
        "companies_total": companies_total,
        "companies_sent": companies_sent,
        "running": running,
        "started_at": started_at,
        "eta": eta,
        "interval": interval,
        "batch": batch,
        "daily_limit": daily_limit,
        "sent_today": sent_today,
        "daily_remaining": max(daily_limit - sent_today, 0) if daily_limit else None,
        "stop_reason": stop_reason,
    }


def build_company_stats(companies=None):
    """Kart istatistiklerinin firma bazli karsiligi.

    Mail bazinda gonderildi/beklemede/basarisiz birbirini disliyor ve toplami
    verir; firma bazinda vermez, cunku bir firmanin bir maili gonderilmisken
    digeri bekliyor olabilir. Bu yuzden her sayac kendi sorusunu yanitlar:
    "kac firmaya tamamen gonderildi", "kac firmada bekleyen mail var".
    """
    companies = database.get_companies() if companies is None else companies
    return {
        "total": len(companies),
        # Firmanin tum mailleri gittiyse gonderilmis sayilir (donus yapanlar dahil).
        "sent": sum(1 for c in companies if c["total"] and c["pending"] == 0 and c["failed"] == 0),
        "replied": sum(1 for c in companies if c["replied"]),
        "pending": sum(1 for c in companies if c["pending"]),
        "failed": sum(1 for c in companies if c["failed"]),
        "disabled": sum(1 for c in companies if not c["enabled"]),
    }


def build_charts(companies=None):
    """Pasta grafiklerinin dilimlerini hazirlar (mail ve firma bazinda).

    Dilimler ayni kirilimi kullanir: gonderilenler geri donus yapan/yapmayan
    diye ikiye ayrilir, ustune bekleyen ve basarisiz eklenir. Boylece her iki
    grafik de butunun tamamini gosterir ve toplamlari stats ile tutar.

    Renkler dogrulanmis kategorik paletten gelir (bkz. templates/index.html).
    """
    stats = database.get_stats()
    replied_mails = stats["replied"]

    companies = database.get_companies() if companies is None else companies
    companies_replied = sum(1 for c in companies if c["replied"])
    # Bir firma ancak tum mailleri gonderildiginde "gonderildi" sayilir.
    companies_sent = sum(
        1 for c in companies
        if c["total"] and c["pending"] == 0 and c["failed"] == 0 and not c["replied"]
    )
    companies_failed = sum(1 for c in companies if c["failed"] and not c["replied"])
    companies_pending = len(companies) - companies_replied - companies_sent - companies_failed

    def slices(replied, sent, pending, failed):
        return [
            {"label": "Geri donus var", "value": replied, "color": "#1baf7a"},
            {"label": "Gonderildi, donus yok", "value": sent, "color": "#2a78d6"},
            {"label": "Beklemede", "value": pending, "color": "#eda100"},
            {"label": "Basarisiz", "value": failed, "color": "#e34948"},
        ]

    return {
        "mails": {
            "title": "Mail bazinda",
            "total": stats["total"],
            "slices": slices(
                replied_mails,
                max(stats["sent"] - replied_mails, 0),
                stats["pending"],
                stats["failed"],
            ),
        },
        "companies": {
            "title": "Firma bazinda",
            "total": len(companies),
            "slices": slices(
                companies_replied, companies_sent, companies_pending, companies_failed
            ),
        },
        "reply_rate": round(replied_mails / stats["sent"] * 100) if stats["sent"] else 0,
    }


@app.route("/")
def index():
    stats = database.get_stats()
    settings = database.get_settings()
    companies = database.get_companies()  # tek sorgu, iki yerde kullanilir
    return render_template(
        "index.html",
        stats=stats,
        company_stats=build_company_stats(companies),
        settings=settings,
        running=sched.is_running(),
        progress=compute_progress(),
        charts=build_charts(companies),
        schedules=database.get_schedules(),
        now=datetime.now(),
    )


@app.route("/progress")
def progress():
    return jsonify(compute_progress())


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        data = {
            "smtp_server": request.form["smtp_server"].strip(),
            "smtp_port": int(request.form["smtp_port"]),
            "smtp_email": request.form["smtp_email"].strip(),
            "smtp_password": request.form["smtp_password"],
            "interval_minutes": int(request.form["interval_minutes"]),
            "batch_size": int(request.form["batch_size"]),
            "subject": request.form["subject"],
            "body": request.form["body"],
            "daily_limit": int(request.form.get("daily_limit") or 0),
        }
        database.save_settings(data)

        for upload in request.files.getlist("attachments"):
            if upload and upload.filename:
                filename = secure_filename(upload.filename)
                filepath = UPLOAD_DIR / filename
                upload.save(filepath)
                database.add_attachment(filename, str(filepath))

        if sched.is_running():
            sched.start()  # reschedule with new interval

        flash("Ayarlar kaydedildi.", "success")
        return redirect(url_for("settings_page"))

    settings = database.get_settings()
    attachments = database.get_attachments()
    return render_template("settings.html", settings=settings, attachments=attachments)


@app.route("/settings/test", methods=["POST"])
def settings_test_connection():
    """Formdaki bilgilerle mail sunucusuna baglanmayi dener (mail gondermeden).

    Degerler formdan gelir, kaydetmeye gerek yoktur. Sifre bos birakilmissa
    kayitli sifre kullanilir.
    """
    data = request.get_json(silent=True) or {}
    server = (data.get("smtp_server") or "").strip()
    email_address = (data.get("smtp_email") or "").strip()
    password = data.get("smtp_password") or ""
    try:
        port = int(data.get("smtp_port") or 587)
    except (TypeError, ValueError):
        port = 587

    if not password:
        saved = database.get_settings()
        password = saved["smtp_password"] or ""

    if not (server and email_address and password):
        return jsonify({"ok": False, "checks": [{
            "name": "Bilgiler", "ok": False,
            "detail": "Sunucu, e-posta ve sifre alanlari dolu olmali.",
        }]})

    checks = []
    try:
        mailer.test_connection(server, port, email_address, password)
        checks.append({
            "name": "SMTP (gonderim)", "ok": True,
            "detail": f"{server}:{port} baglantisi ve girisi basarili.",
        })
    except smtplib.SMTPAuthenticationError as exc:
        checks.append({
            "name": "SMTP (gonderim)", "ok": False,
            "detail": "Kullanici adi/sifre reddedildi. Gmail'de normal sifre degil "
                      f"uygulama sifresi kullanilmali. ({exc.smtp_code})",
        })
    except (OSError, smtplib.SMTPException) as exc:
        checks.append({
            "name": "SMTP (gonderim)", "ok": False,
            "detail": f"{server}:{port} adresine baglanilamadi: {exc}",
        })

    # IMAP sadece Gmail hesaplarinda kullaniliyor (Gmail'den cek/isaretle,
    # geri donus kontrolu); baska saglayicilarda test etmenin anlami yok.
    if "gmail" in server.lower() or email_address.lower().endswith("@gmail.com"):
        try:
            mailbox = gmail_client.test_connection(email_address, password)
            checks.append({
                "name": "IMAP (Gmail okuma)", "ok": True,
                "detail": f"{gmail_client.IMAP_HOST} baglantisi basarili, "
                          f"acilan klasor: {mailbox}",
            })
        except gmail_client.GmailError as exc:
            checks.append({"name": "IMAP (Gmail okuma)", "ok": False, "detail": str(exc)})
    else:
        checks.append({
            "name": "IMAP (Gmail okuma)", "ok": None,
            "detail": "Gmail disi hesap: Gmail'den cekme ve geri donus kontrolu calismaz.",
        })

    return jsonify({"ok": all(c["ok"] for c in checks if c["ok"] is not None), "checks": checks})


@app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(attachment_id):
    attachment = database.get_attachment(attachment_id)
    if attachment:
        try:
            os.remove(attachment["path"])
        except OSError:
            pass
        database.delete_attachment(attachment_id)
        flash("Ek silindi.", "warning")
    return redirect(url_for("settings_page"))


@app.route("/start", methods=["POST"])
def start():
    settings = database.get_settings()
    if not settings["smtp_email"] or not settings["smtp_password"]:
        flash("Once mail ayarlarini girmelisiniz.", "danger")
        return redirect(url_for("index"))
    if sched.daily_limit_reached(settings):
        # Baslatmanin anlami yok: ilk turda gunluk limit yuzunden dururdu.
        flash(
            f"Bugunun gunluk limiti ({settings['daily_limit']} mail) doldu. "
            "Gonderim yarin baslatilabilir.",
            "warning",
        )
        return redirect(url_for("index"))
    sched.start()
    flash("Otomatik gonderim baslatildi.", "success")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    sched.stop()
    flash("Otomatik gonderim durduruldu.", "warning")
    return redirect(url_for("index"))


@app.route("/schedules", methods=["POST"])
def create_schedule():
    """Gonderimin ileri bir tarihte otomatik baslamasini planlar."""
    raw = (request.form.get("start_at") or "").strip()
    try:
        start_at = datetime.fromisoformat(raw)
    except ValueError:
        flash("Gecerli bir tarih/saat secin.", "danger")
        return redirect(url_for("index"))

    settings = database.get_settings()
    if not settings["smtp_email"] or not settings["smtp_password"]:
        flash("Once mail ayarlarini girmelisiniz.", "danger")
        return redirect(url_for("index"))

    _, error = sched.schedule_start(start_at)
    if error:
        flash(error, "danger")
    else:
        flash(
            "Gonderim {} tarihinde baslayacak sekilde zamanlandi.".format(
                start_at.strftime("%d.%m.%Y %H:%M")
            ),
            "success",
        )
    return redirect(url_for("index"))


@app.route("/schedules/<int:schedule_id>/cancel", methods=["POST"])
def cancel_schedule(schedule_id):
    if sched.cancel_schedule(schedule_id):
        flash("Zamanlama iptal edildi.", "warning")
    else:
        flash("Zamanlama bulunamadi ya da zaten calismis.", "info")
    return redirect(url_for("index"))


@app.route("/send-now", methods=["POST"])
def send_now():
    settings = database.get_settings()
    if sched.daily_limit_reached(settings):
        flash(
            f"Bugunun gunluk limiti ({settings['daily_limit']} mail) doldu, "
            "mail gonderilmedi.",
            "warning",
        )
        return redirect(url_for("index"))
    sched.send_batch()
    flash("Bekleyen liste icin gonderim tetiklendi.", "success")
    return redirect(url_for("index"))


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        file = request.files.get("excel_file")
        if not file or file.filename == "":
            flash("Lutfen bir excel dosyasi secin.", "danger")
            return redirect(url_for("import_page"))

        filename = secure_filename(file.filename)
        filepath = UPLOAD_DIR / filename
        file.save(filepath)

        try:
            df = pd.read_excel(filepath)
        except Exception as exc:
            flash(f"Excel okunamadi: {exc}", "danger")
            return redirect(url_for("import_page"))

        columns = {c.lower().strip(): c for c in df.columns}

        email_col = next((columns[c] for c in columns if "mail" in c), None)
        name_col = next(
            (columns[c] for c in columns if "name" in c or "isim" in c or "firma" in c or "ad" in c),
            None,
        )

        if not email_col:
            flash("Excel dosyasinda email sutunu bulunamadi.", "danger")
            return redirect(url_for("import_page"))

        rows = []
        skipped = 0
        for _, row in df.iterrows():
            name = str(row[name_col]).strip() if name_col else ""
            raw = str(row[email_col]).strip()

            for email in raw.split(","):
                email = email.strip()
                if "@" in email:
                    rows.append((name, email))
                else:
                    skipped += 1

        inserted = database.import_contacts(rows)
        duplicates = len(rows) - inserted
        msg = f"{inserted} kisi veritabanina eklendi."
        if duplicates:
            msg += f" {duplicates} mail zaten kayitli oldugu icin eklenmedi."
        if skipped:
            msg += f" {skipped} satirda gecerli mail bulunamadi, atlandi."
        flash(msg, "success")
        return redirect(url_for("contacts_page"))

    return render_template("import.html")


@app.route("/gmail", methods=["GET", "POST"])
def gmail_page():
    """Gmail'de sorgu calistirip yazisilmis sirketleri onizler.

    Yeni kisi eklemez: bulunan adresleri mevcut sirketlerle eslestirir, boylece
    daha once yazisilan firmalar 'gonderildi' olarak isaretlenebilir. Bu adimda
    veritabanina hicbir sey yazilmaz; guncelleme /gmail/mark-sent ile olur.
    Kimlik bilgisi Ayarlar'daki SMTP hesabi/uygulama sifresidir.
    """
    settings = database.get_settings()
    mode = request.form.get("mode") or "query"
    query = (request.form.get("query") or "").strip()
    limit = int(request.form.get("limit") or 200)
    pending_only = request.form.get("pending_only") == "1"
    company_names = [c["name"] for c in database.get_companies()]
    empty = {"settings": settings, "query": query, "limit": limit, "mode": mode,
             "pending_only": pending_only, "company_names": company_names,
             "result": None, "rows": None}

    if request.method == "GET":
        return render_template("gmail.html", **empty)

    try:
        if mode == "scan":
            # Kayitli sirketlerin adreslerini Gmail'de ara. Zaten gonderilmis
            # olanlari taramak gereksiz is oldugu icin varsayilan olarak elenir.
            contacts = database.get_all_contacts()
            addresses = [
                c["email"] for c in contacts
                if not pending_only or c["status"] != "sent"
            ]
            result = gmail_client.scan_addresses(
                settings["smtp_email"], settings["smtp_password"], addresses,
                extra_query=query, limit=limit,
            )
        else:
            result = gmail_client.fetch_addresses(
                settings["smtp_email"], settings["smtp_password"], query, limit
            )
    except gmail_client.GmailError as exc:
        flash(str(exc), "danger")
        return render_template("gmail.html", **empty)

    rows = gmail_client.match_companies(database.get_all_contacts(), result["addresses"])
    return render_template(
        "gmail.html", settings=settings, query=query, limit=limit, mode=mode,
        pending_only=pending_only, company_names=company_names,
        result=result, rows=rows,
    )


@app.route("/gmail/mark-sent", methods=["POST"])
def gmail_mark_sent():
    """Secilen satirlardaki sirketlerin tum maillerini 'gonderildi' isaretler."""
    known = {c["name"] for c in database.get_companies()}
    latest = {}
    unknown = set()

    for index in request.form.getlist("pick"):
        name = (request.form.get(f"company_{index}") or "").strip()
        sent_at = (request.form.get(f"date_{index}") or "").strip()
        if not name:
            continue
        if name not in known:
            # Kullanici listede olmayan bir sirket adi yazmis olabilir; yeni
            # kayit olusturmuyoruz, sadece bildiriyoruz.
            unknown.add(name)
            continue
        # Ayni sirket birden fazla satirda secilebilir; en yeni yazisma tarihi
        # gonderim zamani olarak yazilir.
        if sent_at > latest.get(name, ""):
            latest[name] = sent_at

    if not latest:
        if unknown:
            flash(
                "Secilen sirketler kayitli degil: " + ", ".join(sorted(unknown)),
                "danger",
            )
        else:
            flash("Hicbir sirket secilmedi.", "warning")
        return redirect(url_for("gmail_page"))

    updated = database.mark_companies_sent(list(latest.items()))
    msg = f"{len(latest)} sirketin {updated} maili gonderildi olarak isaretlendi."
    if unknown:
        msg += f" Kayitli olmayan {len(unknown)} sirket adi atlandi."
    flash(msg, "success")
    return redirect(url_for("contacts_page", status="sent"))


@app.route("/contacts")
def contacts_page():
    status = request.args.get("status") or None
    if status not in ("sent", "pending", "failed"):
        status = None
    view = request.args.get("view")
    only_disabled = view == "disabled"
    only_replied = view == "replied"

    companies = database.get_companies(status)
    if only_disabled:
        companies = [c for c in companies if not c["enabled"]]
    elif only_replied:
        companies = [c for c in companies if c["replied"]]

    stats = database.get_stats()
    replied_companies = sum(1 for c in database.get_companies() if c["replied"])
    # Counts reflect the active status filter (all when no filter is set).
    total_companies = len(companies)
    if only_disabled or only_replied:
        total_mails = sum(c["total"] for c in companies)
    else:
        total_mails = stats[status] if status else stats["total"]
    return render_template(
        "contacts.html",
        companies=companies,
        status=status,
        only_disabled=only_disabled,
        only_replied=only_replied,
        stats=stats,
        replied_companies=replied_companies,
        total_companies=total_companies,
        total_mails=total_mails,
    )


@app.route("/contacts/verify", methods=["POST"])
def verify_contacts():
    """Verify every contact's email against the Zeruh API.

    Returns a JSON summary with the ids of non-existing mailboxes so the
    frontend can ask the user whether to delete them.
    """
    contacts = database.get_contact_ids_emails()

    valid = 0
    invalid = 0
    unknown = 0
    invalid_list = []
    error = None

    # Verified in parallel, capped at the API's per-second rate limit.
    results = verifier.verify_emails(contacts)
    for result in results:
        if result["status"] in ("no_api_key", "error") and result["exists"] is None:
            # Surface the first hard error but keep counting the rest as unknown.
            if error is None and result["status"] == "no_api_key":
                error = result["error"]
            unknown += 1
        elif result["exists"] is True:
            valid += 1
        elif result["exists"] is False:
            invalid += 1
            invalid_list.append({"id": result["id"], "email": result["email"]})
        else:
            unknown += 1

    invalid_list.sort(key=lambda c: c["id"])

    return jsonify({
        "total": len(contacts),
        "valid": valid,
        "invalid": invalid,
        "unknown": unknown,
        "invalid_list": invalid_list,
        "invalid_ids": [c["id"] for c in invalid_list],
        "error": error,
    })


@app.route("/contacts/delete-invalid", methods=["POST"])
def delete_invalid_contacts():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    deleted = database.delete_contacts_by_ids(ids)
    return jsonify({"deleted": deleted})


@app.route("/contacts/delete-companies", methods=["POST"])
def delete_companies():
    data = request.get_json(silent=True) or {}
    names = data.get("names", [])
    deleted = database.delete_contacts_by_company_names(names)
    return jsonify({"deleted": deleted})


@app.route("/contacts/toggle-companies", methods=["POST"])
def toggle_companies():
    """Enable/disable the given companies; disabled ones get no mail."""
    data = request.get_json(silent=True) or {}
    names = data.get("names", [])
    enabled = bool(data.get("enabled"))
    updated = database.set_company_enabled(names, enabled)
    return jsonify({"updated": updated, "enabled": enabled})


@app.route("/contacts/check-replies", methods=["POST"])
def check_replies():
    """Mail atilan firmalardan geri donus olup olmadigini Gmail'de kontrol eder.

    Sadece o firmalardan GELEN mailler (from:) aranir ve gonderim tarihinden
    sonra gelenler geri donus sayilir.
    """
    settings = database.get_settings()
    sent = database.get_sent_contacts()
    if not sent:
        flash("Henuz mail gonderilmis firma yok.", "warning")
        return redirect(url_for("contacts_page"))

    try:
        # Cevap genelde yazdigimiz kisiden degil, ayni firmadaki baska birinden
        # gelir; bu yuzden adres degil alan adi aranir (from:abc.com).
        result = gmail_client.scan_addresses(
            settings["smtp_email"], settings["smtp_password"],
            gmail_client.reply_search_terms(sent),
            address_fields=("From",),
        )
    except gmail_client.GmailError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("contacts_page"))

    replies, skipped = gmail_client.match_replies(
        database.get_all_contacts(), result["addresses"]
    )
    updated = database.mark_companies_replied(
        [(r["name"], r["replied_at"]) for r in replies]
    )

    if replies:
        msg = f"{len(replies)} firmadan geri donus bulundu ({updated} kayit guncellendi)."
    else:
        msg = "Mail atilan firmalardan geri donus bulunamadi."
    if skipped:
        # Eksik gorunen bir cevabin nedeni anlasilabilsin diye raporlanir.
        msg += f" {skipped} mail gonderim tarihinden onceye dustugu icin sayilmadi."
    flash(msg, "success" if replies else "info")
    return redirect(url_for("contacts_page", view="replied"))


@app.route("/contacts/dedupe", methods=["POST"])
def dedupe_contacts():
    deleted = database.dedupe_contacts()
    if deleted:
        flash(f"{deleted} tekrar eden kayit silindi.", "warning")
    else:
        flash("Tekrar eden kayit bulunamadi.", "info")
    return redirect(url_for("contacts_page"))


@app.route("/contacts/clear", methods=["POST"])
def clear_contacts():
    database.clear_contacts()
    flash("Kisi listesi temizlendi.", "warning")
    return redirect(url_for("contacts_page"))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
