# Mail Scheduler

Excel'den içe aktarılan şirket/kişi listelerine, belirlenen aralıklarla ve günlük limitlere uyarak otomatik toplu mail gönderen; gönderilen maillere Gmail üzerinden gelen yanıtları tespit edip işaretleyen bir Flask uygulaması.

Soğuk mail (cold mail) kampanyalarını -örneğin iş başvurusu, satış/iş birliği teklifi gibi- takip etmek için tasarlandı: kime ne zaman mail gitti, kim geri döndü, kimden hâlâ cevap bekleniyor, hepsi tek bir panelden görülüyor.

## Özellikler

- **Excel'den içe aktarma** — `.xlsx` dosyasından isim + mail sütunlarını otomatik tanıyıp kişi listesine ekler (sütun adlarında `mail`, `name`/`isim`/`firma`/`ad` gibi kelimeler aranır). Aynı hücrede virgülle ayrılmış birden fazla adres desteklenir, mükerrer kayıtlar atlanır.
- **Otomatik gönderim** — APScheduler ile arka planda çalışan bir iş, ayarlanan dakika aralığında ve parti (batch) büyüklüğünde bekleyen kişilere mail atar. Başarısız gönderimler bir sonraki turda tekrar denenir.
- **Günlük gönderim limiti** — Bir günde atılacak mail sayısına üst sınır konabilir; limit dolduğunda otomatik gönderim kendiliğinden durur ve arayüzde durma nedeni gösterilir.
- **İleri tarihli zamanlama** — Otomatik gönderimin belirli bir tarih/saatte başlaması planlanabilir; uygulama kapalıyken kaçırılan bir başlangıç (1 saatlik tolerans içindeyse) tekrar açılışta çalıştırılır.
- **Anlık gönderim** — Zamanlamayı beklemeden bekleyen kişilere hemen bir parti mail gönderme seçeneği.
- **Ekler (attachments)** — Ayarlar sayfasından dosya yüklenip her mailin ekine eklenebilir (CV, katalog, teklif dosyası vb.).
- **Gmail entegrasyonu (IMAP)** — SMTP ile aynı Gmail hesabı/uygulama şifresi kullanılarak:
  - Kayıtlı kişilerin adresleri Gmail'de aranıp daha önce yazışılmış şirketler tespit edilir ve elle "gönderildi" olarak işaretlenebilir.
  - Serbest metin sorgusuyla (Gmail arama söz dizimi: `from:`, `subject:`, `after:2026/01/01` vb.) IMAP taraması yapılabilir.
  - Mail atılan şirketlerden **geri dönüş var mı** kontrolü: gönderim tarihinden sonra gelen mailler otomatik tespit edilip "cevap geldi" olarak işaretlenir.
- **Mail doğrulama** — Zeruh API ile kişi listesindeki adreslerin gerçekten var olup olmadığı toplu ve paralel olarak (rate-limit'e uyularak) kontrol edilir; geçersiz (undeliverable) adresler listelenip toplu silinebilir.
- **Şirket bazlı yönetim** — Kişiler şirket adına göre gruplanır; bir şirket devre dışı bırakılabilir (mail atlanır), silinebilir, mükerrer kayıtlar temizlenebilir.
- **Dashboard** — Gönderim ilerlemesi (gönderilen/bekleyen/başarısız), tahmini bitiş zamanı, mail ve şirket bazında pasta grafikler, geri dönüş oranı gibi metrikler canlı olarak (`/progress` polling endpoint'i ile) gösterilir.
- **Bağlantı testi** — Ayarlar sayfasından, kaydetmeden önce SMTP ve (Gmail ise) IMAP bağlantısı test edilebilir.

## Teknoloji Yığını

- **Backend:** Python, Flask
- **Zamanlama:** APScheduler (`BackgroundScheduler`)
- **Veritabanı:** SQLite (dosya tabanlı, harici sunucu gerektirmez)
- **Excel işleme:** pandas, openpyxl
- **Mail gönderimi:** `smtplib` (SMTP/SMTP_SSL + STARTTLS)
- **Mail okuma:** `imaplib` (Gmail IMAP, `X-GM-RAW` uzantısıyla Gmail arama söz dizimi)
- **Mail doğrulama:** Zeruh API (`requests`)
- **Frontend:** Flask/Jinja2 şablonları (`templates/`)

## Proje Yapısı

```
mail-scheduler/
├── app.py              # Flask route'ları, dashboard/import/gmail/contacts uçları
├── database.py         # SQLite şeması ve tüm CRUD işlemleri
├── scheduler.py         # APScheduler işleri: periyodik gönderim, zamanlanmış başlangıç, günlük limit
├── mailer.py            # SMTP bağlantısı ve mail gönderimi
├── gmail_client.py       # IMAP ile Gmail tarama, adres/yanıt eşleştirme
├── verifier.py           # Zeruh API ile paralel mail doğrulama
├── templates/            # index, contacts, import, gmail, settings sayfaları
├── uploads/               # İçe aktarılan Excel dosyaları ve ekler
├── Dockerfile / docker-compose.yml
└── requirements.txt
```

## Kurulum

### Gereksinimler

- Python 3.11+
- Gmail üzerinden gönderim/okuma yapılacaksa bir [Gmail uygulama şifresi](https://myaccount.google.com/apppasswords) (2FA açık hesaplarda normal şifre yerine kullanılır)
- (Opsiyonel) Mail doğrulama için bir [Zeruh](https://zeruh.com) API anahtarı

### Yerelde çalıştırma

```bash
git clone https://github.com/Slankss/mail-scheduler.git
cd mail-scheduler

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env-example .env
# .env dosyasına ZERUH_API_KEY değerini girin (mail doğrulama kullanılmayacaksa boş bırakılabilir)

python app.py
```

Uygulama varsayılan olarak `http://localhost:5000` adresinde açılır. Debug modunu açmak için:

```bash
FLASK_DEBUG=1 python app.py
```

### Docker ile çalıştırma

```bash
cp .env-example .env
# .env dosyasına ZERUH_API_KEY değerini girin

docker compose up -d --build
```

Uygulama `http://localhost:8000` üzerinden yayınlanır. Veritabanı ve yüklenen dosyalar, konteyner dışına `./data` klasörüne kalıcı olarak eşlenir.

## Ortam Değişkenleri

| Değişken | Açıklama | Zorunlu |
|---|---|---|
| `ZERUH_API_KEY` | Mail doğrulama için Zeruh API anahtarı | Hayır (sadece "Mail Doğrula" özelliği için) |
| `DATA_DIR` | SQLite veritabanının ve verilerin tutulacağı klasör (varsayılan: proje kökü; Docker'da `/app/data`) | Hayır |
| `FLASK_DEBUG` | `1` verilirse Flask debug modunda çalışır | Hayır |

SMTP/IMAP mail sunucusu bilgileri `.env` dosyasında değil, uygulama içindeki **Ayarlar** sayfasından girilip veritabanında saklanır.

## Kullanım Akışı

1. **Ayarlar** sayfasından SMTP sunucusu, e-posta adresi, uygulama şifresi, mail konusu/gövdesi, gönderim aralığı (dakika), parti büyüklüğü ve (istenirse) günlük gönderim limiti girilir. "Bağlantıyı Test Et" ile SMTP/IMAP doğrulanabilir.
2. **İçe Aktar** sayfasından bir Excel dosyası yüklenerek kişi listesi oluşturulur.
3. İsteğe bağlı olarak **Kişiler** sayfasında liste Zeruh ile doğrulanır, mükerrerler temizlenir, istenmeyen şirketler devre dışı bırakılır/silinir.
4. Ana sayfadan **Başlat** ile otomatik gönderim başlatılır ya da ileri bir tarihe zamanlanır; **Şimdi Gönder** ile bekleyen bir parti anında yollanır.
5. **Gmail** sayfasından, daha önce yazışılmış şirketler taranıp "gönderildi" olarak işaretlenebilir; **Yanıtları Kontrol Et** ile gönderim sonrası gelen cevaplar otomatik tespit edilir.
6. Ana sayfadaki dashboard, ilerleme yüzdesi, tahmini bitiş zamanı ve gönderim/yanıt grafikleriyle sürecin durumunu canlı gösterir.

## Veri Modeli (özet)

- **settings** — SMTP bilgileri, mail şablonu, gönderim aralığı/parti boyutu, günlük limit, zamanlayıcı durumu.
- **contacts** — isim, e-posta, durum (`pending` / `sent` / `failed`), gönderim zamanı, yanıt bilgisi, aktif/pasif durumu.
- **schedules** — ileri tarihli başlangıçların kaydı ve durumu (`pending` / `done` / `cancelled` / `missed` / `failed`).
- **attachments** — mail eklerinin dosya adı ve yolu.

## Lisans

Bu proje için ayrı bir lisans dosyası bulunmamaktadır.
