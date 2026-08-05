#!/usr/bin/env python3
"""Tohum listesindeki alan adları için MX kaydı kontrolü.

Ne doğrular: Alan adı e-posta KABUL EDİYOR MU (MX kaydı var mı, hangi sağlayıcı).
Ne doğrulamaz: ik@ / kariyer@ / cv@ kutusunun GERÇEKTEN VAR OLUP OLMADIĞINI.
  Bunun için SMTP RCPT TO gerekir; bu ortamda 25/tcp kapalı.

MX'i olmayan alan adında dört varyantın dördü de kesin bounce eder — bu kontrol
en azından o satırları önceden eler.

Kullanım:
    pip install dnspython
    python3 arastirma/mx_kontrol.py                      # sonuçları mx_sonuc.json'a yazar
"""

from __future__ import annotations

import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import dns.resolver

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toplayici import kayitli_alan, site_hostu  # noqa: E402

KOK = os.path.dirname(os.path.abspath(__file__))
TOHUM = os.path.join(KOK, "tohum_sirketler.csv")
SONUC = os.path.join(KOK, "mx_sonuc.json")

COZUCU = dns.resolver.Resolver()
COZUCU.timeout = 6
COZUCU.lifetime = 12


def saglayici_tahmini(mx_kayitlari: list[str]) -> str:
    birlesik = " ".join(mx_kayitlari).lower()
    for imza, ad in [
        ("google", "Google Workspace"), ("outlook", "Microsoft 365"),
        ("protection.outlook", "Microsoft 365"), ("yandex", "Yandex"),
        ("zoho", "Zoho"), ("mimecast", "Mimecast"), ("proofpoint", "Proofpoint"),
        ("natrocloud", "Natro"), ("turhost", "Turhost"), ("guzel.net", "Güzel.net"),
        ("secureserver", "GoDaddy"), ("mailgun", "Mailgun"), ("sendgrid", "SendGrid"),
    ]:
        if imza in birlesik:
            return ad
    return ""


def mx_sorgula(alan: str) -> dict:
    try:
        yanit = COZUCU.resolve(alan, "MX")
        kayitlar = sorted((int(r.preference), str(r.exchange).rstrip(".")) for r in yanit)
        mx = [x[1] for x in kayitlar]
        return {"alan": alan, "mx_var": True, "mx": mx, "saglayici": saglayici_tahmini(mx), "not": ""}
    except dns.resolver.NoAnswer:
        return {"alan": alan, "mx_var": False, "mx": [], "saglayici": "", "not": "MX kaydı yok"}
    except dns.resolver.NXDOMAIN:
        return {"alan": alan, "mx_var": False, "mx": [], "saglayici": "", "not": "alan adı yok (NXDOMAIN)"}
    except Exception as e:
        return {"alan": alan, "mx_var": None, "mx": [], "saglayici": "", "not": f"sorgu hatası: {type(e).__name__}"}


def main() -> int:
    with open(TOHUM, newline="", encoding="utf-8") as f:
        satirlar = list(csv.DictReader(f))

    alanlar = {}
    for s in satirlar:
        alan = kayitli_alan(site_hostu(s.get("web_sitesi", "")))
        if alan:
            alanlar[alan] = s.get("firma_adi", "")

    print(f"[i] {len(alanlar)} alan adı sorgulanıyor...")
    with ThreadPoolExecutor(max_workers=10) as havuz:
        sonuclar = list(havuz.map(mx_sorgula, alanlar))

    for r in sonuclar:
        r["firma_adi"] = alanlar[r["alan"]]

    with open(SONUC, "w", encoding="utf-8") as f:
        json.dump(sonuclar, f, ensure_ascii=False, indent=2)

    var = sum(1 for r in sonuclar if r["mx_var"] is True)
    yok = sum(1 for r in sonuclar if r["mx_var"] is False)
    hata = sum(1 for r in sonuclar if r["mx_var"] is None)
    print(f"[✓] {SONUC} yazıldı — MX var: {var} · MX yok: {yok} · sorgu hatası: {hata}")
    for r in sorted(sonuclar, key=lambda x: x["firma_adi"].lower()):
        if r["mx_var"] is not True:
            print(f"    [!] {r['firma_adi']:24s} {r['alan']:26s} {r['not']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
