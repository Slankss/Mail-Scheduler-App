"""Uygulama girisi: kimlik dogrulama, oturum ve CSRF korumasi.

Kullanici adi/sifre .env dosyasindan okunur; veritabaninda kullanici tablosu
yoktur (tek kullanicili bir panel). Sifre tercihen hash olarak saklanir:

    APP_USERNAME=admin
    APP_PASSWORD_HASH=pbkdf2:sha256:...   # python auth.py hash

Duz metin `APP_PASSWORD` da desteklenir (kolay kurulum icin), ama hash tercih
edilir: .env dosyasini goren biri sifreyi dogrudan okuyamaz.

Kimlik bilgileri tanimli degilse uygulama hic acilmaz (bkz. `check_config`).
Bu bilincli bir tercih: yanlis yapilandirmada panelin korumasiz ayaga kalkmasi,
hata verip durmasindan cok daha tehlikeli.
"""
import hmac
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

USERNAME = os.environ.get("APP_USERNAME", "").strip()
_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "").strip()
_PASSWORD_PLAIN = os.environ.get("APP_PASSWORD", "")

# Oturum anahtarlari. Cookie istemcide durdugu icin sadece "giris yapildi"
# bilgisi degil, ne zaman yapildigi da tutulur; mutlak sure asiminda oturum
# cookie hala imzali olsa bile gecersiz sayilir.
_SESSION_USER = "auth_user"
_SESSION_AT = "auth_at"
_SESSION_CSRF = "csrf_token"

# Oturum en fazla bu kadar yasar (yenilenmez); ustune Flask'in
# PERMANENT_SESSION_LIFETIME'i bosta kalma suresini sinirlar.
MAX_SESSION_SECONDS = 12 * 60 * 60

# Kaba kuvvet denemelerini yavaslatan basit sayac (surec bellegi; tek isciyle
# calisan bu uygulama icin yeterli, harici bir bagimlilik gerektirmez).
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
ATTEMPT_WINDOW_SECONDS = 15 * 60

_attempts = {}
_attempts_lock = threading.Lock()

# CSRF kontrolu bu metotlarda calisir; GET/HEAD/OPTIONS durum degistirmez.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class ConfigError(RuntimeError):
    """Kimlik bilgileri eksik ya da hatali."""


def check_config():
    """Kimlik bilgileri yoksa uygulamanin acilmasini engeller."""
    if not USERNAME:
        raise ConfigError(
            "APP_USERNAME tanimli degil. .env dosyasina kullanici adi ekleyin.\n"
            "Ornek: APP_USERNAME=admin"
        )
    if not _PASSWORD_HASH and not _PASSWORD_PLAIN:
        raise ConfigError(
            "APP_PASSWORD_HASH (ya da APP_PASSWORD) tanimli degil.\n"
            "Hash uretmek icin: python auth.py hash"
        )


def verify_credentials(username: str, password: str) -> bool:
    """Kullanici adi + sifre dogrulamasi.

    Kullanici adi da sabit zamanli karsilastirilir; boylece yanit suresinden
    "bu kullanici adi dogru" bilgisi sizmaz.
    """
    if not username or not password:
        return False

    user_ok = hmac.compare_digest(username.strip(), USERNAME)

    if _PASSWORD_HASH:
        try:
            pass_ok = check_password_hash(_PASSWORD_HASH, password)
        except ValueError:
            # Bozuk/eksik hash formati: giris yapilamaz, ama uygulama da
            # sifresiz erisime dusmez.
            pass_ok = False
    else:
        pass_ok = hmac.compare_digest(password, _PASSWORD_PLAIN)

    # Iki kontrol de her zaman calisir (kisa devre yok), sonra birlestirilir.
    return user_ok and pass_ok


# --- Kaba kuvvet korumasi -------------------------------------------------

def _client_ip() -> str:
    return request.remote_addr or "unknown"


def lockout_remaining() -> int:
    """Istemci kilitliyse kalan saniye, degilse 0."""
    with _attempts_lock:
        record = _attempts.get(_client_ip())
        if not record:
            return 0
        remaining = int(record["locked_until"] - time.time())
        return max(remaining, 0)


def record_failure() -> int:
    """Basarisiz denemeyi isler, kilitliyse kalan saniyeyi dondurur."""
    now = time.time()
    ip = _client_ip()
    with _attempts_lock:
        record = _attempts.get(ip)
        # Pencere disinda kalan eski denemeler sayilmaz; birkac gun once yanlis
        # yazan kullanici bugun kilitli baslamasin.
        if not record or now - record["first_at"] > ATTEMPT_WINDOW_SECONDS:
            record = {"count": 0, "first_at": now, "locked_until": 0.0}
        record["count"] += 1
        if record["count"] >= MAX_ATTEMPTS:
            record["locked_until"] = now + LOCKOUT_SECONDS
        _attempts[ip] = record
        return max(int(record["locked_until"] - now), 0)


def reset_failures():
    with _attempts_lock:
        _attempts.pop(_client_ip(), None)


# --- Oturum ---------------------------------------------------------------

def login_user(username: str):
    """Oturumu acar.

    Once session.clear(): giris oncesi olusmus her sey (ozellikle CSRF token)
    atilir, boylece saldirganin onceden yerlestirdigi bir oturum devralinamaz
    (session fixation).
    """
    session.clear()
    session[_SESSION_USER] = username
    session[_SESSION_AT] = datetime.now(timezone.utc).timestamp()
    session[_SESSION_CSRF] = secrets.token_urlsafe(32)
    session.permanent = True


def logout_user():
    session.clear()


def current_user():
    """Oturumdaki kullanici adi; oturum gecersizse None."""
    user = session.get(_SESSION_USER)
    if not user:
        return None
    # .env'deki kullanici adi degistiyse eski cookie ile devam edilemez.
    if user != USERNAME:
        session.clear()
        return None
    started = session.get(_SESSION_AT) or 0
    if datetime.now(timezone.utc).timestamp() - started > MAX_SESSION_SECONDS:
        session.clear()
        return None
    return user


def is_logged_in() -> bool:
    return current_user() is not None


# --- CSRF -----------------------------------------------------------------

def csrf_token() -> str:
    """Oturumun CSRF token'i (yoksa uretilir)."""
    token = session.get(_SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_SESSION_CSRF] = token
    return token


def csrf_ok() -> bool:
    """Istegin CSRF token'i oturumdakiyle uyusuyor mu.

    Form gonderimlerinde gizli alan, fetch cagrilarinda X-CSRF-Token basligi
    kullanilir.
    """
    expected = session.get(_SESSION_CSRF)
    if not expected:
        return False
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    return hmac.compare_digest(sent, expected)


def needs_csrf_check() -> bool:
    return request.method not in _SAFE_METHODS


# --- Istek filtresi -------------------------------------------------------

def wants_json() -> bool:
    """Istek tarayici gezinmesi degil de fetch/API cagrisi mi.

    Boylelerine giris sayfasina yonlendirme yerine 401 donulur; aksi halde
    fetch, giris sayfasinin HTML'ini "basarili yanit" sanar.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] >= accept["text/html"] and accept["application/json"] > 0


def safe_next_target(target: str) -> str:
    """Sadece bu siteye ait goreli yollar kabul edilir (open redirect korumasi)."""
    if not target:
        return ""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return ""
    return target


def deny(reason: str = "Bu islem icin giris yapmalisiniz."):
    """Yetkisiz istegi reddeder: API'ye 401, tarayiciya giris sayfasi."""
    if wants_json():
        return jsonify({"error": reason, "login_required": True}), 401
    target = safe_next_target(request.full_path.rstrip("?")) if request.method == "GET" else ""
    return redirect(url_for("login", next=target) if target else url_for("login"))


def _cli_hash():
    """`python auth.py hash` - .env'ye yazilacak sifre hash'ini uretir."""
    import getpass

    password = getpass.getpass("Sifre: ")
    if not password:
        print("Bos sifre kabul edilmiyor.")
        return 1
    if password != getpass.getpass("Sifre (tekrar): "):
        print("Sifreler eslesmiyor.")
        return 1
    print("\n.env dosyasina ekleyin:\n")
    print(f"APP_PASSWORD_HASH={generate_password_hash(password)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        raise SystemExit(_cli_hash())
    print("Kullanim: python auth.py hash")
    raise SystemExit(1)
