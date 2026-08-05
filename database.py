import os
import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mailer.db"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            smtp_server TEXT,
            smtp_port INTEGER,
            smtp_email TEXT,
            smtp_password TEXT,
            interval_minutes INTEGER DEFAULT 5,
            batch_size INTEGER DEFAULT 1,
            subject TEXT,
            body TEXT,
            scheduler_active INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            sent_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            replied INTEGER NOT NULL DEFAULT 0,
            replied_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            path TEXT NOT NULL
        )
    """)
    # Ensure a single settings row exists
    conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
    # Migration: add columns to existing databases
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "scheduler_started_at" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN scheduler_started_at TEXT")
    if "daily_limit" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN daily_limit INTEGER DEFAULT 0")
    if "stop_reason" not in cols:
        # Gonderim kendiliginden durdugunda (ornegin gunluk limit dolunca)
        # nedeni burada tutulur; kullanici arayuzde neden durdugunu gorur.
        conn.execute("ALTER TABLE settings ADD COLUMN stop_reason TEXT")
    contact_cols = {row["name"] for row in conn.execute("PRAGMA table_info(contacts)")}
    if "enabled" not in contact_cols:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
    if "replied" not in contact_cols:
        conn.execute(
            "ALTER TABLE contacts ADD COLUMN replied INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute("ALTER TABLE contacts ADD COLUMN replied_at TEXT")
    conn.commit()
    conn.close()


def get_settings():
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()
    return row


def save_settings(data):
    conn = get_db_connection()
    conn.execute("""
        UPDATE settings SET
            smtp_server = ?,
            smtp_port = ?,
            smtp_email = ?,
            smtp_password = ?,
            interval_minutes = ?,
            batch_size = ?,
            subject = ?,
            body = ?,
            daily_limit = ?
        WHERE id = 1
    """, (
        data["smtp_server"],
        data["smtp_port"],
        data["smtp_email"],
        data["smtp_password"],
        data["interval_minutes"],
        data["batch_size"],
        data["subject"],
        data["body"],
        data["daily_limit"],
    ))
    conn.commit()
    conn.close()


def set_scheduler_active(active: bool, reason: str = None):
    """Gonderimin calisir/durmus durumunu kaydeder.

    `reason` sadece durdururken anlamlidir: gonderim kendiliginden durduysa
    (gunluk limit dolunca) nedeni saklanir. Baslatirken her zaman temizlenir,
    boylece eski bir neden yeni turda ekranda kalmaz.
    """
    conn = get_db_connection()
    if active:
        conn.execute(
            "UPDATE settings SET scheduler_active = 1, scheduler_started_at = ?, "
            "stop_reason = NULL WHERE id = 1",
            (datetime.now().isoformat(timespec="seconds"),),
        )
    else:
        conn.execute(
            "UPDATE settings SET scheduler_active = 0, stop_reason = ? WHERE id = 1",
            (reason,),
        )
    conn.commit()
    conn.close()


def add_schedule(start_at):
    """Gelecekteki bir baslangic zamani kaydeder ve id'sini dondurur.

    Zamanlamalar veritabaninda tutulur; APScheduler islerini bellekte tuttugu
    icin uygulama yeniden baslarsa kayit olmadan hepsi kaybolurdu.
    """
    conn = get_db_connection()
    cur = conn.execute(
        "INSERT INTO schedules (start_at, status, created_at) VALUES (?, 'pending', ?)",
        (start_at, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    schedule_id = cur.lastrowid
    conn.close()
    return schedule_id


def get_schedules(limit=50):
    """Zamanlamalar: bekleyenler once, sonra en yeni tamamlananlar."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT * FROM schedules
        ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                 CASE status WHEN 'pending' THEN start_at END ASC,
                 start_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_pending_schedules():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM schedules WHERE status = 'pending' ORDER BY start_at"
    ).fetchall()
    conn.close()
    return rows


def set_schedule_status(schedule_id, status, note=None):
    conn = get_db_connection()
    cur = conn.execute(
        "UPDATE schedules SET status = ?, note = ? WHERE id = ? AND status = 'pending'",
        (status, note, schedule_id),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def import_contacts(rows):
    """rows: list of (name, email) tuples.

    Skips emails that already exist in the database (case-insensitive) as well
    as duplicates within the incoming batch. Returns the number of contacts
    actually inserted.
    """
    conn = get_db_connection()
    existing = {
        row[0] for row in conn.execute("SELECT LOWER(TRIM(email)) FROM contacts")
    }
    to_insert = []
    seen = set()
    for name, email in rows:
        key = (email or "").strip().lower()
        if not key or key in existing or key in seen:
            continue
        seen.add(key)
        to_insert.append((name, email))

    conn.executemany(
        "INSERT INTO contacts (name, email, status) VALUES (?, ?, 'pending')",
        to_insert,
    )
    conn.commit()
    conn.close()
    return len(to_insert)


def dedupe_contacts():
    """Remove duplicate contacts that share the same email (case-insensitive).

    When duplicates exist, a 'sent' record is preferred as the one to keep;
    otherwise the oldest record (lowest id) is kept. Returns the number of
    deleted rows.
    """
    conn = get_db_connection()
    rows = conn.execute("SELECT id, email, status FROM contacts").fetchall()

    groups = {}
    for row in rows:
        key = (row["email"] or "").strip().lower()
        groups.setdefault(key, []).append(row)

    def rank(row):
        # Lower sorts first => kept. Prefer 'sent', then the oldest id.
        return (0 if row["status"] == "sent" else 1, row["id"])

    to_delete = []
    for key, items in groups.items():
        if len(items) <= 1:
            continue
        items_sorted = sorted(items, key=rank)
        for row in items_sorted[1:]:
            to_delete.append(row["id"])

    if to_delete:
        placeholders = ",".join("?" for _ in to_delete)
        conn.execute(
            f"DELETE FROM contacts WHERE id IN ({placeholders})", to_delete
        )
        conn.commit()
    conn.close()
    return len(to_delete)


def get_pending_contacts(limit):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM contacts WHERE status = 'pending' AND enabled = 1 LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_sendable_contacts(limit):
    """Return contacts to send to, retrying failed ones first, then pending.

    Failed contacts are ordered before pending ones so that earlier failures
    are re-attempted before new mails go out. Contacts belonging to a disabled
    company (enabled = 0) are never returned.
    """
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT * FROM contacts
        WHERE status IN ('failed', 'pending') AND enabled = 1
        ORDER BY CASE status WHEN 'failed' THEN 0 ELSE 1 END, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def count_sent_today():
    """Number of mails marked sent today (based on local date)."""
    conn = get_db_connection()
    count = conn.execute(
        """
        SELECT COUNT(*) FROM contacts
        WHERE status = 'sent'
          AND sent_at IS NOT NULL
          AND date(sent_at) = date('now', 'localtime')
        """
    ).fetchone()[0]
    conn.close()
    return count


def mark_contact_sent(contact_id):
    conn = get_db_connection()
    conn.execute(
        "UPDATE contacts SET status = 'sent', sent_at = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), contact_id),
    )
    conn.commit()
    conn.close()


def mark_contact_failed(contact_id):
    conn = get_db_connection()
    conn.execute("UPDATE contacts SET status = 'failed' WHERE id = ?", (contact_id,))
    conn.commit()
    conn.close()


def mark_companies_sent(entries):
    """Verilen sirketlerin TUM kayitlarini 'gonderildi' olarak isaretler.

    `entries`: (sirket_adi, sent_at) ciftleri. sent_at, mailin Gmail'deki
    gercek tarihidir; bos birakilirsa su an kullanilir. Zaten 'sent' olan
    kayitlara dokunulmaz, boylece orijinal gonderim zamanlari korunur.

    Gecmise donuk tarih yazmak bilerek tercih edildi: count_sent_today() bugun
    gonderilenleri sayar, eski yazismalar gunluk limiti doldurmamalidir.

    Returns: guncellenen kayit sayisi.
    """
    if not entries:
        return 0
    conn = get_db_connection()
    updated = 0
    for name, sent_at in entries:
        real_name = "" if name == "(Isimsiz)" else name
        cur = conn.execute(
            """
            UPDATE contacts SET status = 'sent', sent_at = ?
            WHERE TRIM(COALESCE(name, '')) = ? AND status != 'sent'
            """,
            (sent_at or datetime.now().isoformat(timespec="seconds"), real_name),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()
    return updated


def mark_companies_replied(entries):
    """Verilen sirketlerin tum kayitlarini 'geri donus yapti' olarak isaretler.

    `entries`: (sirket_adi, replied_at) ciftleri. Geri donus firma bazinda
    tutulur: firmanin hangi adresi cevap yazarsa yazsin firma donus yapmis
    sayilir. Zaten isaretli kayitlarin tarihi korunur.

    Returns: guncellenen kayit sayisi.
    """
    if not entries:
        return 0
    conn = get_db_connection()
    updated = 0
    for name, replied_at in entries:
        real_name = "" if name == "(Isimsiz)" else name
        cur = conn.execute(
            """
            UPDATE contacts SET replied = 1, replied_at = ?
            WHERE TRIM(COALESCE(name, '')) = ? AND replied = 0
            """,
            (replied_at or datetime.now().isoformat(timespec="seconds"), real_name),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()
    return updated


def get_sent_contacts():
    """Mail gonderilmis kayitlar (geri donus taramasinin hedefi)."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM contacts WHERE status = 'sent' ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def get_all_contacts(state: str = None):
    conn = get_db_connection()
    if state is not None:
        rows = conn.execute("SELECT * FROM contacts WHERE status = ? ORDER BY id DESC", (state,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    conn.close()
    return rows


def get_companies(state: str = None):
    """Group contacts by company name.

    Returns a list of dicts:
    {name, contacts, total, sent, pending, failed, disabled, enabled}.
    When `state` is given, only contacts with that status are listed, and
    companies with no matching contact are excluded.

    `enabled` is False as soon as one of the company's contacts is disabled;
    set_company_enabled() always writes every contact of a company at once, so
    a mixed state only shows up for hand-edited databases.
    """
    contacts = get_all_contacts(state)
    grouped = {}
    for c in contacts:
        key = (c["name"] or "").strip() or "(Isimsiz)"
        company = grouped.setdefault(
            key,
            {
                "name": key,
                "contacts": [],
                "total": 0,
                "sent": 0,
                "pending": 0,
                "failed": 0,
                "disabled": 0,
                "enabled": True,
                "replied": 0,
                "replied_at": None,
            },
        )
        company["contacts"].append(c)
        company["total"] += 1
        if c["status"] in ("sent", "pending", "failed"):
            company[c["status"]] += 1
        if not c["enabled"]:
            company["disabled"] += 1
            company["enabled"] = False
        if c["replied"]:
            company["replied"] += 1
            if (c["replied_at"] or "") > (company["replied_at"] or ""):
                company["replied_at"] = c["replied_at"]
    return sorted(grouped.values(), key=lambda x: x["name"].lower())


def set_company_enabled(names, enabled: bool):
    """Enable/disable every contact of the given companies.

    Disabled companies are skipped by the scheduler. get_companies() groups
    blank names under "(Isimsiz)", so that placeholder is mapped back to an
    empty string here. Returns the number of updated contacts.
    """
    if not names:
        return 0
    conn = get_db_connection()
    updated = 0
    for name in names:
        real_name = "" if name == "(Isimsiz)" else name
        cur = conn.execute(
            "UPDATE contacts SET enabled = ? WHERE TRIM(COALESCE(name, '')) = ?",
            (1 if enabled else 0, real_name),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()
    return updated


def add_attachment(filename, path):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO attachments (filename, path) VALUES (?, ?)", (filename, path)
    )
    conn.commit()
    conn.close()


def get_attachments():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM attachments ORDER BY id").fetchall()
    conn.close()
    return rows


def get_attachment(attachment_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    conn.close()
    return row


def delete_attachment(attachment_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
    conn.commit()
    conn.close()


def get_stats(only_enabled: bool = False):
    """Contact counts per status.

    With `only_enabled`, contacts of disabled companies are left out so that
    progress / ETA are computed over the mails that will actually be sent.
    `disabled` always counts every disabled contact.
    """
    conn = get_db_connection()
    active = "enabled = 1" if only_enabled else "1 = 1"
    total = conn.execute(f"SELECT COUNT(*) FROM contacts WHERE {active}").fetchone()[0]
    sent = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE status = 'sent' AND {active}"
    ).fetchone()[0]
    pending = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE status = 'pending' AND {active}"
    ).fetchone()[0]
    failed = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE status = 'failed' AND {active}"
    ).fetchone()[0]
    disabled = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE enabled = 0"
    ).fetchone()[0]
    replied = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE replied = 1 AND {active}"
    ).fetchone()[0]
    conn.close()
    return {
        "total": total,
        "sent": sent,
        "pending": pending,
        "failed": failed,
        "disabled": disabled,
        "replied": replied,
    }


def clear_contacts():
    conn = get_db_connection()
    conn.execute("DELETE FROM contacts")
    conn.commit()
    conn.close()


def get_contact_ids_emails():
    """Return all contacts as a list of (id, email) tuples."""
    conn = get_db_connection()
    rows = conn.execute("SELECT id, email FROM contacts ORDER BY id").fetchall()
    conn.close()
    return [(row["id"], row["email"]) for row in rows]


def delete_contacts_by_ids(ids):
    """Delete the contacts whose ids are in the given iterable."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    conn = get_db_connection()
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", ids)
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_contacts_by_company_names(names):
    """Delete every contact belonging to the given company names.

    get_companies() groups contacts under "(Isimsiz)" when the name is blank,
    so that placeholder is mapped back to an empty string for matching here.
    """
    if not names:
        return 0
    conn = get_db_connection()
    deleted = 0
    for name in names:
        real_name = "" if name == "(Isimsiz)" else name
        cur = conn.execute(
            "DELETE FROM contacts WHERE TRIM(COALESCE(name, '')) = ?", (real_name,)
        )
        deleted += cur.rowcount
    conn.commit()
    conn.close()
    return deleted
