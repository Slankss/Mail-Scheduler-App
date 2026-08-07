import hashlib
import json
import math
import os
import smtplib
from datetime import datetime, timedelta

import pandas as pd
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

import auth
import crypto
import database
import gmail_client
import mailer
import scheduler as sched
import verifier

UPLOAD_DIR = database.DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Kimlik bilgileri eksikse uygulama hic acilmaz: korumasiz bir panelin ayakta
# olmasindansa acilista hata vermek yeglenir.
auth.check_config()

app = Flask(__name__)
# Oturum cookie'sinin imza anahtari .env'deki APP_SECRET_KEY'den turetilir
# (sifreleme anahtarindan farkli bir turetim, ayni sir iki ise ayni bicimde
# girmesin diye). Sabit bir anahtar kodda tutulmaz: bilen herkes gecerli
# oturum cookie'si uretebilirdi.
app.secret_key = hashlib.sha256(b"session:" + crypto.secret_material()).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JavaScript cookie'yi okuyamaz
    SESSION_COOKIE_SAMESITE="Lax",  # baska siteden gelen POST'lar cookie tasimaz
    # HTTPS arkasinda calisirken .env'de SESSION_COOKIE_SECURE=1 yapin; localhost
    # (http) icin acik birakilirsa oturum hic kurulamaz.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),  # bosta kalma suresi
    SESSION_REFRESH_EACH_REQUEST=True,
)

database.init_db()
# APScheduler isleri bellekte durur; kayitli zamanlamalar her acilista kurulur.
sched.restore_schedules()

# Girise izin verilen tek uclar. Liste "izin verilenler" seklinde tutulur:
# yeni bir route eklendiginde varsayilan olarak korumali olur, korumasiz degil.
PUBLIC_ENDPOINTS = {"login", "static"}


@app.before_request
def require_login():
    """Her istekten once oturum ve CSRF kontrolu.

    Tek tek route'lara dekorator koymak yerine global bir filtre kullanilir:
    ileride eklenecek bir ucun korumasiz kalmasi mumkun olmasin diye.
    """
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if not auth.is_logged_in():
        return auth.deny()

    # Oturum acikken de CSRF gerekir: baska bir sitedeki form/istek, tarayicidaki
    # gecerli oturumu kullanarak mail gonderimi baslatmasin.
    if auth.needs_csrf_check() and not auth.csrf_ok():
        if auth.wants_json():
            return jsonify({"error": "Gecersiz ya da eksik CSRF token."}), 400
        flash("Guvenlik dogrulamasi basarisiz (CSRF). Sayfayi yenileyip tekrar deneyin.", "danger")
        return redirect(url_for("index"))

    return None


@app.context_processor
def inject_auth():
    """Sablonlarda csrf_token() ve current_user kullanilabilsin."""
    return {"csrf_token": auth.csrf_token, "current_user": auth.current_user()}


@app.after_request
def security_headers(response):
    """Temel tarayici korumalari."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")  # clickjacking
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.is_logged_in():
        return redirect(url_for("index"))

    if request.method == "POST":
        # Giris formu de CSRF token tasir: baska bir site, kullaniciyi farkinda
        # olmadan kendi belirledigi bir hesaba giris yaptiramasin.
        if not auth.csrf_ok():
            flash("Oturum dogrulanamadi. Sayfayi yenileyip tekrar deneyin.", "danger")
            return render_template("login.html"), 400

        locked = auth.lockout_remaining()
        if locked:
            flash(
                f"Cok fazla hatali deneme. {locked // 60 + 1} dakika sonra tekrar deneyin.",
                "danger",
            )
            return render_template("login.html"), 429

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if auth.verify_credentials(username, password):
            auth.reset_failures()
            auth.login_user(username.strip())
            target = auth.safe_next_target(request.args.get("next", ""))
            return redirect(target or url_for("index"))

        locked = auth.record_failure()
        # Hangi alanin yanlis oldugu soylenmez: kullanici adi tahmini
        # kolaylasmasin.
        if locked:
            flash(
                f"Cok fazla hatali deneme. Giris {locked // 60 + 1} dakika kilitlendi.",
                "danger",
            )
        else:
            flash("Kullanici adi ya da sifre hatali.", "danger")
        return render_template("login.html"), 401

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    flash("Cikis yapildi.", "info")
    return redirect(url_for("login"))


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
    variants = database.get_mail_variant_rows()
    return render_template(
        "settings.html", settings=settings, attachments=attachments, variants=variants
    )


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


@app.route("/settings/variants/add", methods=["POST"])
def add_variant():
    prefix = request.form.get("prefix", "")
    if database.add_mail_variant(prefix):
        flash(f"'{prefix.strip().lower()}' varyant olarak eklendi.", "success")
    else:
        flash("Varyant eklenemedi (bos birakilmis olabilir ya da zaten kayitli).", "warning")
    return redirect(url_for("settings_page"))


@app.route("/settings/variants/<int:variant_id>/delete", methods=["POST"])
def delete_variant(variant_id):
    if database.delete_mail_variant(variant_id):
        flash("Varyant silindi.", "warning")
    return redirect(url_for("settings_page"))


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


@app.route("/settings/export", methods=["GET"])
def settings_export():
    """Ayarlari JSON dosyasi olarak indirir.

    smtp_password sifreli haliyle aktarilir (bkz. database.export_settings);
    ancak dosyayi baska bir kurulumda ice aktarirken APP_SECRET_KEY farkliysa
    sifre cozulemez, kullanici sifreyi elle girmelidir.
    """
    data = database.export_settings()
    payload = {
        "type": "mail-scheduler-settings",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "settings": data or {},
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    filename = f"mail-scheduler-settings-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/settings/import", methods=["POST"])
def settings_import():
    """Daha once /settings/export ile indirilmis bir JSON dosyasini ice aktarir."""
    upload = request.files.get("settings_file")
    if not upload or not upload.filename:
        flash("Ice aktarmak icin bir dosya secin.", "danger")
        return redirect(url_for("settings_page"))

    try:
        payload = json.load(upload.stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        flash("Dosya gecerli bir JSON degil.", "danger")
        return redirect(url_for("settings_page"))

    settings_data = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_data, dict):
        flash("Dosya beklenen formatta degil (settings alani bulunamadi).", "danger")
        return redirect(url_for("settings_page"))

    database.import_settings(settings_data)

    if sched.is_running():
        sched.start()  # reschedule with the imported interval

    flash("Ayarlar ice aktarildi.", "success")
    return redirect(url_for("settings_page"))


@app.route("/db/export", methods=["GET"])
def db_export():
    """Tum veritabanini (ayarlar, kisiler, zamanlamalar, ek dosya kayitlari)
    ham SQLite dosyasi olarak indirir.

    Ayarlar disa aktarimindan (/settings/export) farki: o sadece ayarlari JSON
    olarak verir, bu ise kisi listesi ve zamanlamalar dahil her seyi tasir -
    tam yedek/tasima icin kullanilir. Yuklenen ek dosyalarin kendisi (data/uploads)
    dahil degildir, sadece isim/yol kaydi.
    """
    filename = f"mail-scheduler-db-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    return send_file(
        database.export_db_path(),
        as_attachment=True,
        download_name=filename,
        mimetype="application/x-sqlite3",
    )


@app.route("/db/import", methods=["POST"])
def db_import():
    """Daha once /db/export ile indirilmis bir SQLite dosyasini ice aktarir.

    Otomatik gonderim calisiyorsa once durdurulur: ice aktarilan veri farkli
    bir kisi listesi/ayar tasiyabilir, kullanicinin yeni durumu gormeden
    gonderimin devam etmesi istenmez. Bellekteki zamanlanmis isler de
    sched.reset() ile yeni veritabanindan yeniden kurulur.
    """
    upload = request.files.get("db_file")
    if not upload or not upload.filename:
        flash("Ice aktarmak icin bir dosya secin.", "danger")
        return redirect(url_for("settings_page"))

    was_running = sched.is_running()
    if was_running:
        sched.stop()

    try:
        backup_name = database.import_db(upload.stream)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("settings_page"))

    sched.reset()

    msg = "Veritabani ice aktarildi."
    if backup_name:
        msg += f" Onceki veritabani '{backup_name}' olarak yedeklendi."
    if was_running:
        msg += " Otomatik gonderim guvenlik icin durduruldu, kontrol edip isterseniz tekrar baslatin."
    flash(msg, "warning" if was_running else "success")
    return redirect(url_for("index"))


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


def extract_domain(value):
    """"acme.com", "https://www.acme.com/", "info@acme.com" -> "acme.com"."""
    value = value.strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value.startswith("www."):
        value = value[4:]
    value = value.split("/")[0]
    return value.strip(".")


def derive_company_name(domain):
    """"acme-corp.com" -> "Acme Corp". Sirket ismi verilmediginde kullanilir."""
    label = domain.split(".")[0] if domain else ""
    label = label.replace("-", " ").replace("_", " ").strip()
    return label.title()


def build_manual_company_rows(text, variants):
    """Manuel sirket ekleme formundaki metni (name, email) satirlarina cevirir.

    Her satir bir sirket: "Isim; domain-ya-da-mail" ya da isimsiz olarak sadece
    "domain-ya-da-mail". Varyant tanimliysa (Ayarlar) domain'e her varyant icin
    bir adres uretilir; boylece tek bir domain girmek yeterli olur. Varyant
    yoksa: girilen deger zaten tam bir mail adresiyse oldugu gibi, sadece
    domain'se "info@domain" olarak eklenir.

    Returns: (rows, company_count, skipped_count).
    """
    rows = []
    company_count = 0
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ";" in line:
            name, _, value = line.partition(";")
            name = name.strip()
            value = value.strip()
        else:
            name = ""
            value = line

        if not value:
            skipped += 1
            continue

        has_email = "@" in value
        domain = extract_domain(value)
        if not domain or "." not in domain:
            skipped += 1
            continue

        if not name:
            name = derive_company_name(domain)

        if variants:
            emails = [f"{variant}@{domain}" for variant in variants]
        elif has_email:
            emails = [value]
        else:
            emails = [f"info@{domain}"]

        rows.extend((name, email) for email in emails)
        company_count += 1

    return rows, company_count, skipped


@app.route("/companies/add", methods=["GET", "POST"])
def add_companies_page():
    """Excel yuklemeden, elle tek ya da bir liste sirket eklemek icin."""
    variants = database.get_mail_variants()

    if request.method == "POST":
        text = request.form.get("companies_text", "")
        rows, company_count, skipped = build_manual_company_rows(text, variants)

        if not rows:
            flash("Eklenecek gecerli bir satir bulunamadi.", "danger")
            return redirect(url_for("add_companies_page"))

        inserted = database.import_contacts(rows)
        duplicates = len(rows) - inserted
        msg = f"{company_count} sirket icin {inserted} mail adresi eklendi."
        if duplicates:
            msg += f" {duplicates} adres zaten kayitli oldugu icin eklenmedi."
        if skipped:
            msg += f" {skipped} satir okunamadi, atlandi."
        flash(msg, "success" if inserted else "warning")
        return redirect(url_for("contacts_page"))

    return render_template("add_company.html", variants=variants)


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
