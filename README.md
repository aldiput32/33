# Safelink Bypass Bot (Python)

Bot Python fleksibel untuk bypass/extract URL asli dari berbagai jenis safelink dan URL shortener.

## Fitur

- **Base64 decode** — Blogspot safelink, custom safelink dengan Base64
- **URL parameter extraction** — `?url=`, `?link=`, `?target=`, `?redirect=`, dll.
- **HTTP redirect follow** — bit.ly, tinyurl, s.id, dan semua shortener
- **HTML/JS parsing** — `window.location`, `meta refresh`, `atob()`, dll.
- **Recursive bypass** — Ikuti chain redirect sampai URL final
- **Batch processing** — Proses banyak URL sekaligus dari file
- **Interactive mode** — Mode interaktif untuk coba-coba

## Install

```bash
pip install -r requirements.txt
```

## Penggunaan

### Single URL
```bash
python safelink_bypass.py "https://example.com/safelink?url=aHR0cHM6Ly9nb29nbGUuY29t"
```

### Recursive (ikuti semua redirect chain)
```bash
python safelink_bypass.py --recursive "https://bit.ly/xyz"
```

### Batch (dari file)
```bash
python safelink_bypass.py --batch urls.txt
```

### Interactive Mode
```bash
python safelink_bypass.py --interactive
```

### Verbose (tampilkan chain)
```bash
python safelink_bypass.py --verbose --recursive "https://shortener.com/abc"
```

## Contoh Output

```
[+] Original : https://blog.example.com/p/safelink.html?url=aHR0cHM6Ly9kcml2ZS5nb29nbGUuY29tL2ZpbGU=
[+] Result   : https://drive.google.com/file
[+] Method   : url_param_decode
```

## Supported Services

| Tipe | Contoh |
|------|--------|
| Blogspot Safelink | `blogspot.com/p/generate.html?url=BASE64` |
| URL Shortener | bit.ly, tinyurl.com, s.id, cutt.ly, dll. |
| Custom Safelink | Any `?url=`, `?link=`, `?target=` parameter |
| JS Redirect | `window.location.href`, `document.location` |
| Meta Refresh | `<meta http-equiv="refresh">` |
| Base64 in Path | `/go/BASE64`, `/redirect/BASE64` |
| Base64 in Hash | `#BASE64ENCODEDURL` |

## Extensible

Tambahkan pattern baru dengan mudah:

```python
from safelink_bypass import SafelinkBypass

bot = SafelinkBypass()

# Tambah custom URL param
bot.URL_PARAMS.append("my_custom_param")

# Tambah custom safelink pattern
bot.SAFELINK_PATTERNS.append(r"mysite\.com/link/([A-Za-z0-9+/=]+)")

# Bypass
result = bot.bypass("https://mysite.com/link/aHR0cHM6Ly9leGFtcGxlLmNvbQ==")
print(result.final_url)
```

## Tanpa Dependencies (Mode Offline)

Bot tetap bisa bypass Base64/URL-param tanpa install `requests`:
```bash
python safelink_bypass.py "https://safelink.com?url=aHR0cHM6Ly9nb29nbGUuY29t"
```
(Hanya HTTP redirect/HTML parse yang butuh `requests` + `beautifulsoup4`)
