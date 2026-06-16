import math
import os
from datetime import datetime, timedelta

import pandas as pd
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

import database
import scheduler as sched
import verifier

UPLOAD_DIR = database.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = "mail-scheduler-secret-key"

database.init_db()


def compute_progress():
    """Build a progress snapshot for the dashboard / polling endpoint."""
    stats = database.get_stats()
    settings = database.get_settings()
    running = sched.is_running()

    total = stats["total"]
    sent = stats["sent"]
    failed = stats["failed"]
    pending = stats["pending"]
    processed = sent + failed

    percent = round(processed / total * 100) if total else 0

    companies = database.get_companies()
    companies_total = len(companies)
    # A company counts as "sent" once all of its mails have gone out
    # (no pending and no failed left).
    companies_sent = sum(
        1 for c in companies if c["total"] > 0 and c["pending"] == 0 and c["failed"] == 0
    )

    interval = settings["interval_minutes"] or 5
    batch = settings["batch_size"] or 1

    started_at = settings["scheduler_started_at"] if running else None

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
        "processed": processed,
        "percent": percent,
        "companies_total": companies_total,
        "companies_sent": companies_sent,
        "running": running,
        "started_at": started_at,
        "eta": eta,
        "interval": interval,
        "batch": batch,
    }


@app.route("/")
def index():
    stats = database.get_stats()
    settings = database.get_settings()
    return render_template(
        "index.html",
        stats=stats,
        settings=settings,
        running=sched.is_running(),
        progress=compute_progress(),
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
    sched.start()
    flash("Otomatik gonderim baslatildi.", "success")
    return redirect(url_for("index"))


@app.route("/stop", methods=["POST"])
def stop():
    sched.stop()
    flash("Otomatik gonderim durduruldu.", "warning")
    return redirect(url_for("index"))


@app.route("/send-now", methods=["POST"])
def send_now():
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


@app.route("/contacts")
def contacts_page():
    status = request.args.get("status") or None
    if status not in ("sent", "pending", "failed"):
        status = None
    companies = database.get_companies(status)
    stats = database.get_stats()
    # Counts reflect the active status filter (all when no filter is set).
    total_companies = len(companies)
    total_mails = stats[status] if status else stats["total"]
    return render_template(
        "contacts.html",
        companies=companies,
        status=status,
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
