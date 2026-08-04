"""Gmail'den yazisilan adresleri IMAP uzerinden ceker ve sirketlerle eslestirir.

Gmail'in `X-GM-RAW` IMAP uzantisi, web arayuzundeki arama sozdiziminin
aynisini kabul eder (from:, subject:, has:attachment, after:2026/01/01 ...),
bu yuzden kullanicinin yazdigi sorgu oldugu gibi sunucuya gonderilir.

Adresler From, To ve Cc basliklarindan toplanir (kullanicinin kendi adresi
haric): boylece hem "ben bu firmaya yazdim" (in:sent -> To) hem de "firma bana
yazdi" (From) sorgulari ayni sekilde calisir.

Kimlik dogrulama SMTP ile ayni uygulama sifresini kullanir; ayri bir Google
Cloud projesi ya da OAuth akisi gerekmez. Ileride Gmail API'ye gecilmek
istenirse sadece bu modul degisir: disariya `fetch_addresses()` ve
`match_companies()` verilir.
"""

import email
import imaplib
import re
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Tek FETCH komutuna sigdirilacak mesaj sayisi; cok uzun komutlar sunucu
# tarafindan reddedilebiliyor.
FETCH_CHUNK = 200

# Tum sirketleri tararken tek SEARCH komutunda birlestirilecek adres sayisi.
# 50 adres ~2 KB'lik bir komut satiri eder; IMAP satir sinirlarinin altinda kalir.
ADDRESS_CHUNK = 50

# Sirket adi cikarmaya calismanin anlamsiz oldugu genel posta saglayicilari.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "yahoo.com", "yandex.com", "yandex.com.tr", "icloud.com", "aol.com",
    "mail.ru", "protonmail.com", "windowslive.com", "msn.com",
}

# no-reply@, noreply-..., bounce@ gibi cevap alinamayan adresler.
NOREPLY_RE = re.compile(r"(^|[.\-_])(no-?reply|donotreply|bounce|mailer-daemon|postmaster)([.\-_]|$)", re.I)


class GmailError(Exception):
    """Kullaniciya gosterilebilecek, anlasilir Gmail/IMAP hatasi."""


def _decode(value):
    """MIME ile kodlanmis basligi (=?UTF-8?B?...?=) duz metne cevirir."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _display_name(raw_name):
    """From basligindaki gorunen adi dondurur; yoksa bos string.

    "info@abc.com" gibi adres tekrari olan ad alanlari sirket adi sayilmaz.
    """
    name = _decode(raw_name).strip().strip('"').strip()
    return name if name and "@" not in name else ""


def _name_from_domain(addr):
    """Gorunen ad yoksa sirket adini alan adindan turetir."""
    domain = addr.partition("@")[2].lower()
    if not domain or domain in FREE_MAIL_DOMAINS:
        return ""
    # www.abc.com.tr -> abc
    parts = [p for p in domain.split(".") if p not in ("www", "com", "net", "org", "co", "tr", "io")]
    return (parts[0] if parts else domain).replace("-", " ").title()


def _select_all_mail(imap):
    """\\All ozniteligine sahip klasoru (Tum Postalar) salt-okunur acar.

    Klasor adi hesabin diline gore degistigi icin isimle degil, IMAP'in
    dondurdugu oznitelikle bulunur; bulunamazsa INBOX'a duser.
    """
    mailbox = "INBOX"
    try:
        typ, lines = imap.list()
        if typ == "OK":
            for line in lines:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                if "\\All" in text:
                    mailbox = '"%s"' % text.rsplit(' "/" ', 1)[-1].strip().strip('"')
                    break
    except imaplib.IMAP4.error:
        pass
    typ, _ = imap.select(mailbox, readonly=True)  # readonly: mailler okundu isaretlenmez
    if typ != "OK":
        imap.select("INBOX", readonly=True)
    return mailbox


def _search(imap, query):
    """X-GM-RAW ile arama yapar, UID listesini (yeniden eskiye) dondurur."""
    quoted = '"%s"' % query.replace("\\", "\\\\").replace('"', '\\"')
    try:
        if not quoted.isascii():
            # imaplib komutlari varsayilan olarak ASCII kodlar; Turkce karakterli
            # sorgular icin sunucudan UTF-8 kabulu istenir.
            try:
                imap.enable("UTF8=ACCEPT")
            except Exception:
                raise GmailError(
                    "Sunucu Turkce karakterli sorgu kabul etmedi. "
                    "Sorguyu ASCII karakterlerle yazmayi deneyin."
                )
        typ, data = imap.uid("SEARCH", "X-GM-RAW", quoted)
    except imaplib.IMAP4.error as exc:
        raise GmailError(f"Arama basarisiz: {exc}")
    if typ != "OK":
        raise GmailError("Arama basarisiz oldu, sorguyu kontrol edin.")
    uids = (data[0] or b"").split()
    uids.reverse()  # en yeni mailler once
    return uids


def _connect(imap_email, imap_password):
    """Gmail'e baglanip giris yapar; hatalari anlasilir mesaja cevirir."""
    if not imap_email or not imap_password:
        raise GmailError("Once Ayarlar sayfasinda Gmail adresi ve uygulama sifresi kayitli olmali.")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=60)
    except OSError as exc:
        raise GmailError(f"Gmail'e baglanilamadi: {exc}")
    try:
        imap.login(imap_email, imap_password)
    except imaplib.IMAP4.error as exc:
        try:
            imap.logout()
        except Exception:
            pass
        raise GmailError(
            "Gmail girisi basarisiz. Hesapta 2 adimli dogrulama acik olmali ve "
            f"SMTP sifresi bir 'uygulama sifresi' olmali. ({exc})"
        )
    return imap


def test_connection(imap_email, imap_password):
    """IMAP'e baglanip giris yapmayi ve posta kutusunu acmayi dener.

    Sadece giris yetmez: IMAP hesap ayarlarindan kapatilmis olabilir, bu ancak
    bir klasor secilmeye calisilinca ortaya cikar. Basarisizsa GmailError.
    """
    imap = _connect(imap_email, imap_password)
    try:
        mailbox = _select_all_mail(imap)
        return mailbox.strip('"')
    except imaplib.IMAP4.error as exc:
        raise GmailError(f"Posta kutusu acilamadi (IMAP kapali olabilir): {exc}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _collect(imap, own_address, uids, into, address_fields=("From", "To", "Cc")):
    """Verilen maillerin basliklarindaki adresleri `into` sozlugune biriktirir.

    Sadece basliklar cekilir (BODY.PEEK ile, okundu isareti birakmadan);
    `address_fields` basliklarindaki adresler alinir, hesabin kendi adresi
    elenir. Geri donus taramasinda sadece "From" kullanilir: firma A'nin
    maili firma B'yi Cc'ye almissa B cevap yazmis sayilmamalidir.
    """
    for start in range(0, len(uids), FETCH_CHUNK):
        chunk = b",".join(uids[start:start + FETCH_CHUNK])
        typ, data = imap.uid(
            "FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])"
        )
        if typ != "OK":
            continue
        for part in data:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            headers = email.message_from_bytes(part[1])
            subject = _decode(headers.get("Subject"))
            date = headers.get("Date")
            try:
                date = parsedate_to_datetime(date).isoformat(timespec="seconds") if date else ""
            except (TypeError, ValueError):
                date = ""

            participants = getaddresses(
                [headers.get(field, "") for field in address_fields]
            )
            for raw_name, addr in participants:
                addr = (addr or "").strip().lower()
                if "@" not in addr or addr == own_address:
                    continue
                display = _display_name(raw_name)
                record = into.get(addr)
                if record is None:
                    record = into[addr] = {
                        "name": display or _name_from_domain(addr),
                        "email": addr,
                        "subject": subject,
                        "date": date,
                        "count": 0,
                        "has_display_name": bool(display),
                        "noreply": bool(NOREPLY_RE.search(addr.partition("@")[0])),
                        "free_domain": addr.partition("@")[2] in FREE_MAIL_DOMAINS,
                    }
                elif display and not record["has_display_name"]:
                    # Ayni gonderici bazen adsiz mail atiyor; gercek ad
                    # alan adindan turetilmis isme her zaman tercih edilir.
                    record["name"] = display
                    record["has_display_name"] = True
                record["count"] += 1
                # En yeni mailin konusu/tarihi gosterilsin.
                if date and date > (record["date"] or ""):
                    record["subject"], record["date"] = subject, date


def _finalize(collected, matched):
    results = sorted(collected.values(), key=lambda s: (-s["count"], s["email"]))
    for record in results:
        record.pop("has_display_name", None)
    return {"matched": matched, "addresses": results}


def fetch_addresses(imap_email, imap_password, query, limit=200):
    """Sorguya uyan maillerde yazisilan adresleri toplar.

    Donen: {"matched": taranan mail sayisi,
            "addresses": [{name, email, subject, date, count, ...}]}
    """
    if not (query or "").strip():
        raise GmailError("Bir arama sorgusu girmelisiniz.")

    imap = _connect(imap_email, imap_password)
    try:
        _select_all_mail(imap)
        uids = _search(imap, query.strip())[: max(1, int(limit))]
        collected = {}
        _collect(imap, imap_email.strip().lower(), uids, collected)
        return _finalize(collected, len(uids))
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def reply_search_terms(contacts):
    """Geri donus aramasi icin sorgu terimleri uretir - oncelikle ALAN ADI.

    Cevap cogunlukla yazdigimiz kisiden degil, ayni sirketteki baska birinden
    gelir (info@abc.com'a yazariz, ahmet@abc.com cevaplar). Bu yuzden adres
    yerine alan adi aranir: `from:abc.com` o firmadaki herkesi yakalar.

    gmail.com gibi genel saglayicilarda alan adi aranmaz - `from:gmail.com`
    alakasiz binlerce maili getirirdi; oradaki kayitlar icin tam adres aranir.
    """
    terms = []
    seen = set()
    for contact in contacts:
        addr = (contact["email"] or "").strip().lower()
        if "@" not in addr:
            continue
        domain = addr.partition("@")[2]
        term = "from:" + (addr if domain in FREE_MAIL_DOMAINS else domain)
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def scan_addresses(imap_email, imap_password, terms, extra_query="",
                   limit=5000, address_fields=("From", "To", "Cc")):
    """Verilen arama terimlerini Gmail'de toplu olarak arar.

    `terms`: hazir sorgu parcalari - duz adres ("a@x.com") ya da alan adi
    aramasi ("from:abc.com"). 970 firma icin tek tek arama yapmak dakikalar
    surerdi; bunun yerine terimler ADDRESS_CHUNK'luk gruplara bolunup Gmail'in
    OR sozdizimiyle tek sorguda aratilir. `extra_query` verilirse her gruba
    eklenir (orn. "after:2026/01/01").

    Ayni mail birden fazla grupla eslesebilecegi icin UID'ler tekilllestirilir,
    yoksa mail sayilari iki katina cikardi.
    """
    known = [t.strip().lower() for t in terms if t and t.strip()]
    if not known:
        raise GmailError("Veritabaninda aranacak mail adresi yok.")

    imap = _connect(imap_email, imap_password)
    try:
        _select_all_mail(imap)
        own_address = imap_email.strip().lower()
        collected = {}
        seen = set()
        extra = (extra_query or "").strip()

        for start in range(0, len(known), ADDRESS_CHUNK):
            group = known[start:start + ADDRESS_CHUNK]
            query = "(%s)" % " OR ".join(group)
            if extra:
                query = f"{query} {extra}"
            uids = [uid for uid in _search(imap, query) if uid not in seen]
            seen.update(uids)
            if len(seen) > limit:
                # Guvenlik siniri: cok buyuk kutularda istek sonsuza kadar surmesin.
                uids = uids[: max(0, limit - (len(seen) - len(uids)))]
                _collect(imap, own_address, uids, collected, address_fields)
                break
            _collect(imap, own_address, uids, collected, address_fields)

        result = _finalize(collected, len(seen))
        result["scanned"] = len(known)
        return result
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def match_companies(contacts, addresses):
    """Gmail'de bulunan her adres icin bir sirket onerisi hesaplar.

    `contacts`: name/email/status alanlari olan kayitlar (get_all_contacts()).
    `addresses`: fetch_addresses() ciktisindaki adres kayitlari.

    Eslestirme once tam adresle, tutmazsa alan adiyla yapilir (satis@abc.com
    ile info@abc.com ayni firmadir); gmail, hotmail gibi genel saglayicilarda
    alan adi eslestirmesi yapilmaz, yoksa alakasiz firmalar birbirine karisir.
    Ayni adres birden fazla sirkete kayitliysa tam adres eslesmesi olan, esitlik
    halinde alfabetik ilk sirket onerilir; kullanici tabloda degistirebilir.

    Oneri sadece bir baslangic degeridir: kullanici satirdaki sirketi elle
    secip eslestirebilir, bu yuzden eslesmeyen adresler de dondurulur.

    Donen: her adres icin {..., company, match_type, company_total,
    company_pending} alanlari eklenmis kayit listesi. match_type: "email",
    "domain" ya da bos (eslesme yok).
    """
    by_email = {}
    by_domain = {}
    companies = {}
    for contact in contacts:
        company = (contact["name"] or "").strip() or "(Isimsiz)"
        stats = companies.setdefault(company, {"total": 0, "sent": 0, "pending": 0})
        stats["total"] += 1
        if contact["status"] == "sent":
            stats["sent"] += 1
        else:
            stats["pending"] += 1

        addr = (contact["email"] or "").strip().lower()
        if "@" not in addr:
            continue
        by_email.setdefault(addr, set()).add(company)
        domain = addr.partition("@")[2]
        if domain and domain not in FREE_MAIL_DOMAINS:
            by_domain.setdefault(domain, set()).add(company)

    rows = []
    for record in addresses:
        addr = record["email"]
        hits = by_email.get(addr)
        match_type = "email"
        if not hits:
            hits = by_domain.get(addr.partition("@")[2])
            match_type = "domain"

        row = dict(record)
        if hits:
            company = sorted(hits)[0]
            row["company"] = company
            row["match_type"] = match_type
            row["company_total"] = companies[company]["total"]
            row["company_pending"] = companies[company]["pending"]
        else:
            row["company"] = ""
            row["match_type"] = ""
            row["company_total"] = row["company_pending"] = 0
        rows.append(row)

    # Eslesenler once, icinde bekleyen maili olanlar en ustte.
    rows.sort(key=lambda r: (not r["company"], not r["company_pending"], r["email"]))
    return rows


def _as_naive_local(value):
    """ISO tarihi saat dilimsiz yerel zamana cevirir; cevrilemezse None.

    Gmail tarihleri saat dilimli ("+03:00"), veritabanindaki sent_at ise
    dilimsiz yazilir; ikisini dogrudan karsilastirmak TypeError verir.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def match_replies(contacts, addresses):
    """Mail attigimiz firmalardan gelen cevaplari bulur.

    Karsilastirma firmanin EN ERKEN gonderim tarihine ve ">=" ile yapilir.
    Nedeni: "Gmail'den Isaretle" akisi sent_at olarak firmayla olan en yeni
    yazismanin tarihini yazar; o yazisma firmanin cevabinin kendisi olabilir.
    Kural "kesinlikle sonra" olsaydi cevap kendi tarihine takilip elenirdi.

    Gonderimden onceki yazismalar (biz yazmadan once onlarin bize yazmasi)
    yine donus sayilmaz, ama sayilari raporlanir - bir cevap gorunmuyorsa
    sebebinin bu eleme oldugu anlasilabilsin diye. Mail gonderilmemis firmalar
    hic degerlendirilmez.

    Donen: (replies, skipped)
      replies -> [{name, replied_at, email, sent_at}] firma basina en yeni cevap
      skipped -> gonderim tarihinden onceye dustugu icin elenen mail sayisi
    """
    sent_at = {}
    for contact in contacts:
        if contact["status"] != "sent":
            continue
        company = (contact["name"] or "").strip() or "(Isimsiz)"
        when = _as_naive_local(contact["sent_at"])
        if when and when < sent_at.get(company, when.max):
            sent_at[company] = when

    replies = {}
    skipped = 0
    for row in match_companies(contacts, addresses):
        company = row["company"]
        if not company or company not in sent_at:
            continue
        when = _as_naive_local(row["date"])
        if when is None:
            continue
        if when < sent_at[company]:
            skipped += 1
            continue
        current = replies.get(company)
        if current is None or row["date"] > current["replied_at"]:
            replies[company] = {
                "name": company,
                "replied_at": row["date"],
                "email": row["email"],
                "sent_at": sent_at[company].isoformat(timespec="seconds"),
            }

    ordered = sorted(replies.values(), key=lambda r: r["replied_at"], reverse=True)
    return ordered, skipped
