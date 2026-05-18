#!/usr/bin/env python3
"""
Bot Tiket ExploreFomo
- SEMUA config di data_pembeli.txt (termasuk event_url, lokasi, payment, war_time)
- TANPA pertanyaan apapun di terminal
- Langsung: python fomo_bot.py
- Paralel otomatis
- Retry tanpa jeda
- Ctrl+C langsung ringkasan

Format data_pembeli.txt:
    NAMA|NIK|EMAIL|NO_WA|QTY|EVENT_URL|LOKASI|PG_METHOD|WAR_TIME

Cara pakai:
    1. Edit data_pembeli.txt
    2. python fomo_bot.py   <-- langsung jalan, 0 pertanyaan
"""

import os
import re
import sys
import time
import signal
import requests
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

WIB = timezone(timedelta(hours=7))

# =============================================================================
SITE_BASE = "https://sites.explorefomo.id"
FASPAY_BASE = "https://xpress.faspay.co.id/v4"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RETRY = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "data_pembeli.txt")

print_lock = threading.Lock()
stop_flag = threading.Event()
results = []
results_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)
        sys.stdout.flush()


def create_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# =============================================================================
# DATA LOADER
# =============================================================================

def load_data_pembeli():
    """Load dari data_pembeli.txt
    Format: NAMA|NIK|EMAIL|NO_WA|QTY|EVENT_URL|LOKASI|PG_METHOD|WAR_TIME
    """
    if not os.path.exists(DATA_FILE):
        safe_print(f"\n  [ERROR] File tidak ditemukan: {DATA_FILE}")
        safe_print(f"  Format: NAMA|NIK|EMAIL|NO_WA|QTY|EVENT_URL|LOKASI|PG_METHOD|WAR_TIME")
        safe_print(f"  Contoh: BUDI|330123|budi@mail.com|08123|1|https://sites.explorefomo.id/Event|1|711|13:00:00")
        sys.exit(1)

    data = []
    with open(DATA_FILE, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 6:
                safe_print(f"  [WARNING] Baris {line_num} kurang field (min 6): {line[:80]}")
                continue

            qty = 1
            if len(parts) >= 5 and parts[4].strip().isdigit():
                qty = max(1, min(3, int(parts[4].strip())))

            event_url = parts[5].strip() if len(parts) > 5 else ""
            if not event_url.startswith("http"):
                event_url = f"{SITE_BASE}/{event_url}" if event_url else ""

            lokasi = int(parts[6].strip()) if len(parts) > 6 and parts[6].strip().isdigit() else 1
            pg_method = parts[7].strip() if len(parts) > 7 and parts[7].strip() else "711"
            war_time = parts[8].strip() if len(parts) > 8 else ""

            data.append({
                "nama": parts[0].strip(),
                "nik": parts[1].strip(),
                "email": parts[2].strip(),
                "wa": parts[3].strip(),
                "qty": qty,
                "event_url": event_url,
                "lokasi": lokasi,
                "pg_method": pg_method,
                "war_time": war_time,
            })

    if not data:
        safe_print(f"\n  [ERROR] Tidak ada data di {DATA_FILE}")
        sys.exit(1)
    return data


# =============================================================================
# HTML HELPERS
# =============================================================================

def extract_csrf_token(html):
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m: return m.group(1)
    m = re.search(r'value="([^"]+)"\s+name="_token"', html)
    if m: return m.group(1)
    return None

def extract_hidden_fields(html):
    fields = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        nm = re.search(r'name=["\']([^"\']+)["\']', tag)
        vm = re.search(r'value=["\']([^"\']*)["\']', tag)
        if nm: fields[nm.group(1)] = vm.group(1) if vm else ""
    return fields

def extract_qris_info(html):
    info = {}
    m = re.search(r'Amount to Pay.*?IDR\s*([\d.,]+)', html, re.DOTALL)
    if m: info["amount"] = m.group(1).strip()
    m = re.search(r'Expired Date.*?(\d{2}/\d{2}/\d{4}\s*\|\s*[\d:]+)', html, re.DOTALL)
    if m: info["expired"] = m.group(1).strip()
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]+class=["\']qris-card-qr["\']', html)
    if not m: m = re.search(r'class=["\']qris-card-qr["\'][^>]+src=["\']([^"\']+)["\']', html)
    if m: info["qr_url"] = m.group(1)
    m = re.search(r'id=["\']trx_id["\'][^>]+value=["\']([^"\']+)["\']', html)
    if m: info["trx_id"] = m.group(1)
    return info

def extract_email_params(html):
    params = {}
    m = re.search(r'id=["\']trx_id["\'][^>]+value=["\']([^"\' ]+)["\']', html)
    if m: params["trx_uid"] = m.group(1)
    if not params.get("trx_uid"):
        m = re.search(r"trx_uid['\",:\s]+['\"]?([\d]+)", html)
        if m: params["trx_uid"] = m.group(1)
    m = re.search(r'id=["\']channel_uid["\'][^>]+value=["\']([^"\' ]+)["\']', html)
    if m: params["channel_uid"] = m.group(1)
    if not params.get("channel_uid"):
        m = re.search(r"channel_uid['\",:\s]+['\"]?([\d]+)", html)
        if m: params["channel_uid"] = m.group(1)
    m = re.search(r"merchant_name['\",:\s]+['\"]?([^'\"\),]+)", html)
    if m: params["merchant_name"] = m.group(1).strip()
    m = re.search(r"boi_uid['\",:\s]+['\"]?([\d]+)", html)
    if m: params["boi_uid"] = m.group(1)
    m = re.search(r"total_pay['\",:\s]+['\"]?([\d]+)", html)
    if m: params["total_pay"] = m.group(1)
    m = re.search(r"bill_expired['\",:\s]+['\"]([^'\"]+)", html)
    if m: params["bill_expired"] = m.group(1).strip()
    m = re.search(r"customer['\",:\s]+['\"]([^'\"]+@[^'\"]+)", html)
    if m: params["customer"] = m.group(1).strip()
    m = re.search(r"color['\",:\s]+['\"]([a-fA-F0-9]{6})", html)
    if m: params["color"] = m.group(1)
    if params.get("channel_uid"): params["channel"] = params["channel_uid"]
    return params

def extract_order_no(html):
    m = re.search(r'Order No.*?<td[^>]*class="[^"]*text-end[^"]*"[^>]*>([^<]+)', html, re.DOTALL)
    if m: return m.group(1).strip()
    m = re.search(r'Nomor Pesanan.*?<td[^>]*>([^<]+)', html, re.DOTALL)
    if m: return m.group(1).strip()
    return ""

def extract_total_amount(html):
    m = re.search(r'Grand Total.*?<td[^>]*>([^<]+)', html, re.DOTALL)
    if m: return m.group(1).strip()
    return ""


# =============================================================================
# REQUEST HELPER
# =============================================================================

def req(session, method, url, data=None, retry=MAX_RETRY, allow_redirects=True,
        headers_extra=None):
    for attempt in range(1, retry + 1):
        try:
            kw = {"timeout": 30, "allow_redirects": allow_redirects}
            if headers_extra:
                h = dict(session.headers)
                h.update(headers_extra)
                kw["headers"] = h
            if method == "GET":
                r = session.get(url, **kw)
            else:
                kw["data"] = data
                r = session.post(url, **kw)
            return r
        except requests.exceptions.RequestException as e:
            if attempt < retry:
                time.sleep(0.2)
            else:
                return None


# =============================================================================
# PRINT SUMMARY
# =============================================================================

def print_summary():
    print(f"\n\n{'='*60}")
    print(f"  RINGKASAN AKHIR")
    print(f"{'='*60}")
    for r in results:
        s = "OK" if r.get("ok") else "GAGAL"
        print(f"  [{s}] {r['nama']}")
        if r.get("ok"):
            print(f"        Order  : {r.get('order_no', '-')}")
            print(f"        Total  : {r.get('total', '-')}")
            if r.get("qr_url"): print(f"        QR     : {r['qr_url']}")
            if r.get("expired"): print(f"        Exp    : {r['expired']}")
            if r.get("payment_url"): print(f"        URL    : {r['payment_url']}")
            print(f"        Email  : {'YA' if r.get('email_sent') else 'TIDAK'}")
        else:
            print(f"        Error  : {r.get('error', '?')}")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n  {ok} berhasil, {len(results)-ok} gagal dari {len(results)} pembeli")
    print(f"{'='*60}\n")


# =============================================================================
# PURCHASE FLOW
# =============================================================================

def run_purchase(pembeli, pembeli_num, total):
    """Beli tiket 1 pembeli. Retry sampai dapat atau di-stop."""
    prefix = f"  [{pembeli_num}/{total}]"
    event_url = pembeli["event_url"]
    lokasi = pembeli["lokasi"]
    qty = pembeli["qty"]
    pg_method = pembeli["pg_method"]
    channel_code = pg_method
    attempt = 0
    session = create_session()

    while not stop_flag.is_set():
        attempt += 1
        safe_print(f"\n{'='*60}")
        safe_print(f"{prefix} {pembeli['nama']} | Qty={qty} | Attempt #{attempt}")
        safe_print(f"{'='*60}")

        # STEP 1: GET event
        safe_print(f"{prefix} [1/7] GET event...", end=" ")
        r = req(session, "GET", event_url)
        if not r or r.status_code != 200:
            safe_print(f"GAGAL"); continue
        safe_print("OK")

        csrf = extract_csrf_token(r.text)
        if not csrf:
            safe_print(f"{prefix} [!] No CSRF, retry..."); continue

        # Admin fee
        base_price = 0
        pm = re.search(r'name="harga"[^>]*value="(\d+)"', r.text)
        if pm: base_price = int(pm.group(1))
        subtotal = base_price * qty
        admin_fee = 0
        if subtotal > 0:
            fr = req(session, "POST", f"{SITE_BASE}/api/adminfeeTicket",
                     data={"pg_code": pg_method, "harga": subtotal}, retry=2)
            if fr and fr.status_code == 200:
                try: admin_fee = int(fr.text.strip())
                except: pass
        total_amount = subtotal + admin_fee + 1000

        # STEP 2: POST form
        safe_print(f"{prefix} [2/7] Submit form...", end=" ")
        form = {"_token": csrf, "nama": pembeli["nama"], "no_identitas": pembeli["nik"],
                "email": pembeli["email"], "wa": pembeli["wa"], "lokasi": str(lokasi),
                "harga": str(base_price), "qty": str(qty), "pg_method": str(pg_method),
                "total": str(total_amount)}
        r = req(session, "POST", event_url.rstrip("/") + "/payment", data=form,
                headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                               "Referer": event_url, "Origin": SITE_BASE})
        if not r or r.status_code not in (200, 301, 302, 303):
            safe_print("GAGAL"); continue
        safe_print(f"OK ({r.status_code})")
        cur_url, html = r.url, r.text

        # STEP 3: Faspay summary
        safe_print(f"{prefix} [3/7] Faspay summary...", end=" ")
        order_no = ""
        if "faspay" in cur_url.lower() or "ANT-FOMO" in html:
            order_no = extract_order_no(html)
            safe_print(f"OK ({order_no})")
            wa = pembeli["wa"]
            if wa.startswith("0"): wa = wa[1:]
            elif wa.startswith("+62"): wa = wa[3:]
            elif wa.startswith("62"): wa = wa[2:]
            r = req(session, "POST", f"{FASPAY_BASE}/payment/checkout",
                    data={"lang": "en", "tel": wa, "country_phone": "+62"},
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded", "Referer": cur_url})
            if not r or r.status_code not in (200, 301, 302, 303):
                safe_print(f"{prefix} WA submit GAGAL"); continue
            cur_url, html = r.url, r.text
        else:
            safe_print("SKIP")

        # STEP 4: Checkout channel
        safe_print(f"{prefix} [4/7] Channel {channel_code}...", end=" ")
        if "channel_list" in html or "Payment Method" in html:
            r = req(session, "POST", f"{FASPAY_BASE}/payment/order",
                    data={"pglist_rad": channel_code, "txtTerm": "on", "checkout": ""},
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded", "Referer": cur_url})
            if not r or r.status_code not in (200, 301, 302, 303):
                safe_print("GAGAL"); continue
            safe_print("OK"); cur_url, html = r.url, r.text
        else:
            safe_print("SKIP")

        # STEP 5: Confirm
        safe_print(f"{prefix} [5/7] Confirm...", end=" ")
        if "pglist_rad" in html and "checkout" in html:
            hf = extract_hidden_fields(html)
            pd = {"pglist_rad": hf.get("pglist_rad", channel_code), "txtTerm": "on",
                  "checkout": hf.get("checkout", "1")}
            pm2 = re.search(r'name="payment_plan\[\]"[^>]*value="([^"]*)"', html)
            if pm2: pd["payment_plan[]"] = pm2.group(1)
            total_str = extract_total_amount(html)
            if not order_no: order_no = extract_order_no(html)
            safe_print(f"OK ({total_str})")
            r = req(session, "POST", f"{FASPAY_BASE}/payment/order", data=pd,
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded", "Referer": cur_url})
            if not r or r.status_code not in (200, 301, 302, 303):
                safe_print(f"{prefix} Pay GAGAL"); continue
            cur_url, html = r.url, r.text
        else:
            safe_print("SKIP"); total_str = ""

        # STEP 6: Payment page
        safe_print(f"{prefix} [6/7] Payment...", end=" ")
        pi = extract_qris_info(html)
        if not order_no: order_no = extract_order_no(html)
        if not total_str: total_str = pi.get("amount", extract_total_amount(html))

        if pi.get("qr_url") or pi.get("trx_id") or "payment" in cur_url.lower():
            safe_print("OK")
            safe_print(f"{prefix} BERHASIL! Order={order_no} Total={total_str}")
            if pi.get("qr_url"): safe_print(f"{prefix} QR: {pi['qr_url']}")
            if pi.get("expired"): safe_print(f"{prefix} Exp: {pi['expired']}")
            safe_print(f"{prefix} URL: {cur_url}")

            # STEP 7: Email
            safe_print(f"{prefix} [7/7] Email...", end=" ")
            ep = extract_email_params(html)
            trx = ep.get("trx_uid", pi.get("trx_id", ""))
            email_sent = False
            if trx:
                ch = ep.get("channel_uid", channel_code)
                mn = ep.get("merchant_name", "ANT-FOMO")
                ed = {"trx_uid": trx, "channel_uid": ch, "language": "en",
                      "merchant_name": mn, "total_pay": ep.get("total_pay", ""),
                      "custEmail": pembeli["email"], "bill_expired": ep.get("bill_expired", ""),
                      "channel": ch, "customer": ep.get("customer", pembeli["email"]),
                      "boi": mn, "boi_uid": ep.get("boi_uid", ""), "color": ep.get("color", "fd9c41")}
                er = req(session, "POST", f"{FASPAY_BASE}/payment/sendemailpdf", data=ed,
                         headers_extra={"Content-Type": "application/x-www-form-urlencoded", "Referer": cur_url}, retry=3)
                if er and er.status_code == 200:
                    safe_print("OK"); email_sent = True
                else:
                    safe_print("GAGAL")
            else:
                safe_print("SKIP")

            return {"nama": pembeli["nama"], "ok": True, "email": pembeli["email"],
                    "order_no": order_no, "total": total_str,
                    "qr_url": pi.get("qr_url", ""), "expired": pi.get("expired", ""),
                    "payment_url": cur_url, "email_sent": email_sent}
        else:
            safe_print("GAGAL"); continue

    return {"nama": pembeli["nama"], "ok": False, "error": "Dihentikan paksa"}


# =============================================================================
# MAIN - 0 PERTANYAAN
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  BOT TIKET EXPLOREFOMO")
    print("  0 pertanyaan - semua dari data_pembeli.txt")
    print("  Ctrl+C = stop + ringkasan")
    print("=" * 60)

    all_pembeli = load_data_pembeli()

    print(f"\n  Loaded: {len(all_pembeli)} pembeli")
    for i, p in enumerate(all_pembeli, 1):
        print(f"    {i}. {p['nama']} | qty={p['qty']} | {p['event_url'][:50]} | {p['pg_method']} | war={p['war_time'] or 'NOW'}")

    # Countdown ke war_time (ambil dari pembeli pertama, atau yg paling awal)
    war_time_str = ""
    for p in all_pembeli:
        if p["war_time"]:
            war_time_str = p["war_time"]
            break

    war_target = None
    if war_time_str:
        try:
            parts = war_time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            s = int(parts[2]) if len(parts) > 2 else 0
            now = datetime.now(WIB)
            war_target = now.replace(hour=h, minute=m, second=s, microsecond=0)
            if war_target <= now:
                war_target += timedelta(days=1)
        except (ValueError, IndexError):
            pass

    if war_target:
        print(f"\n  COUNTDOWN KE {war_target.strftime('%H:%M:%S')} WIB...")
        while True:
            now = datetime.now(WIB)
            diff = (war_target - now).total_seconds()
            if diff <= 0: break
            h = int(diff // 3600); m = int((diff % 3600) // 60); s = int(diff % 60)
            sys.stdout.write(f"\r  \u23F1  {h:02d}:{m:02d}:{s:02d} ... ")
            sys.stdout.flush()
            time.sleep(0.5)
        print(f"\n\n  GAS!!!\n")
    else:
        print(f"\n  War Time: LANGSUNG GAS\n")

    print(f"  >> MULAI BELI TIKET (paralel, retry tanpa jeda)...\n")

    stop_flag.clear()
    results.clear()

    def sig_handler(sig, frame):
        stop_flag.set()
        raise KeyboardInterrupt
    old_h = signal.signal(signal.SIGINT, sig_handler)

    executor = None
    def on_done(fut, p):
        try: r = fut.result()
        except Exception as e: r = {"nama": p["nama"], "ok": False, "error": str(e)}
        with results_lock: results.append(r)

    try:
        total = len(all_pembeli)
        if total > 1:
            executor = ThreadPoolExecutor(max_workers=total)
            for i, p in enumerate(all_pembeli, 1):
                f = executor.submit(run_purchase, p, i, total)
                f.add_done_callback(lambda fut, pp=p: on_done(fut, pp))
            for f in as_completed([]):
                pass
            # Wait for all
            executor.shutdown(wait=True)
        else:
            r = run_purchase(all_pembeli[0], 1, 1)
            with results_lock: results.append(r)

    except KeyboardInterrupt:
        safe_print(f"\n\n  [!] STOP PAKSA...")
        stop_flag.set()
    finally:
        signal.signal(signal.SIGINT, old_h)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.3)
        with results_lock:
            done = {r["nama"] for r in results}
            for p in all_pembeli:
                if p["nama"] not in done:
                    results.append({"nama": p["nama"], "ok": False, "error": "Dihentikan paksa"})
        print_summary()


if __name__ == "__main__":
    main()
