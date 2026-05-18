#!/usr/bin/env python3
"""
Bot Tiket ExploreFomo
- Config dari config.txt (tanpa pertanyaan)
- Data pembeli dari data_pembeli.txt (NAMA|NIK|EMAIL|WA|QTY)
- Langsung run tanpa input apapun
- Payment otomatis via Faspay (QRIS / channel lain)
- Support paralel atau sequential
- Retry logic tanpa jeda
- Ctrl+C langsung ringkasan

Cara pakai:
    1. Edit config.txt (event url, lokasi, pembayaran, jam war)
    2. Edit data_pembeli.txt (data pembeli + qty)
    3. python fomo_bot.py   <-- langsung jalan, tanpa tanya apapun
"""

import json
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
# KONFIGURASI
# =============================================================================
SITE_BASE = "https://sites.explorefomo.id"
FASPAY_BASE = "https://xpress.faspay.co.id/v4"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RETRY = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.txt")
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
# CONFIG & DATA LOADER
# =============================================================================

def load_config():
    """Load config dari config.txt"""
    if not os.path.exists(CONFIG_FILE):
        safe_print(f"\n  [ERROR] File config tidak ditemukan: {CONFIG_FILE}")
        safe_print(f"  Buat file config.txt, contoh:")
        safe_print(f"  EVENT_URL=https://sites.explorefomo.id/VikingFest2026")
        safe_print(f"  LOKASI=1")
        safe_print(f"  PG_METHOD=711")
        safe_print(f"  WAR_TIME=")
        safe_print(f"  MODE=parallel")
        safe_print(f"  AKUN=all")
        sys.exit(1)

    config = {}
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    return config


def load_data_pembeli():
    """Load data pembeli dari data_pembeli.txt
    Format: NAMA|NIK|EMAIL|NO_WA|QTY
    """
    if not os.path.exists(DATA_FILE):
        safe_print(f"\n  [ERROR] File data pembeli tidak ditemukan: {DATA_FILE}")
        safe_print(f"  Format: NAMA|NIK|EMAIL|NO_WA|QTY")
        safe_print(f"  Contoh: BUDI SANTOSO|3301234567890001|budi@email.com|081234567890|1")
        sys.exit(1)

    data = []
    with open(DATA_FILE, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                safe_print(f"  [WARNING] Baris {line_num} format salah: {line}")
                continue
            qty = 1
            if len(parts) >= 5 and parts[4].strip().isdigit():
                qty = max(1, min(3, int(parts[4].strip())))
            data.append({
                "nama": parts[0].strip(),
                "nik": parts[1].strip(),
                "email": parts[2].strip(),
                "wa": parts[3].strip(),
                "qty": qty,
            })

    if not data:
        safe_print(f"\n  [ERROR] Tidak ada data pembeli di {DATA_FILE}")
        sys.exit(1)
    return data


def select_pembeli(all_pembeli, choice):
    """Select pembeli berdasarkan choice string"""
    choice = choice.strip().lower()
    if choice in ("all", "semua", ""):
        return all_pembeli
    elif "-" in choice:
        try:
            parts = choice.split("-")
            start = int(parts[0]) - 1
            end = int(parts[1])
            return all_pembeli[start:end]
        except (ValueError, IndexError):
            return all_pembeli
    elif "," in choice:
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",")]
            return [all_pembeli[i] for i in indices if 0 <= i < len(all_pembeli)]
        except (ValueError, IndexError):
            return all_pembeli
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(all_pembeli):
                return [all_pembeli[idx]]
        except ValueError:
            pass
        return all_pembeli


# =============================================================================
# HTML HELPERS
# =============================================================================

def extract_csrf_token(html):
    match = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if match:
        return match.group(1)
    match = re.search(r'value="([^"]+)"\s+name="_token"', html)
    if match:
        return match.group(1)
    return None


def extract_hidden_fields(html):
    fields = {}
    for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = match.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        value_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            fields[name_m.group(1)] = value_m.group(1) if value_m else ""
    return fields


def extract_payment_channels(html):
    channels = []
    for match in re.finditer(
        r'<input[^>]+name=["\']channel_list["\'][^>]*value=["\'](\d+)["\'][^>]*channel=["\']([^"\']+)["\']', html):
        channels.append({"code": match.group(1), "name": match.group(2)})
    if not channels:
        for match in re.finditer(r'value=["\'](\d+)["\'][^>]*channel=["\']([^"\']+)["\']', html):
            channels.append({"code": match.group(1), "name": match.group(2)})
    return channels


def extract_qris_info(html):
    info = {}
    match = re.search(r'Amount to Pay.*?IDR\s*([\d.,]+)', html, re.DOTALL)
    if match:
        info["amount"] = match.group(1).strip()
    match = re.search(r'Expired Date.*?(\d{2}/\d{2}/\d{4}\s*\|\s*[\d:]+)', html, re.DOTALL)
    if match:
        info["expired"] = match.group(1).strip()
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\'][^>]+class=["\']qris-card-qr["\']', html)
    if not match:
        match = re.search(r'class=["\']qris-card-qr["\'][^>]+src=["\']([^"\']+)["\']', html)
    if match:
        info["qr_url"] = match.group(1)
    match = re.search(r'id=["\']trx_id["\'][^>]+value=["\']([^"\']+)["\']', html)
    if match:
        info["trx_id"] = match.group(1)
    match = re.search(r'checkStatus', html)
    if match:
        info["has_check_status"] = True
    return info


def extract_email_params(html):
    params = {}
    m = re.search(r'id=["\']trx_id["\'][^>]+value=["\']([^"\' ]+)["\']', html)
    if m:
        params["trx_uid"] = m.group(1)
    if not params.get("trx_uid"):
        m = re.search(r"trx_uid['\",:\s]+['\"]?([\d]+)", html)
        if m:
            params["trx_uid"] = m.group(1)
    m = re.search(r'id=["\']channel_uid["\'][^>]+value=["\']([^"\' ]+)["\']', html)
    if m:
        params["channel_uid"] = m.group(1)
    if not params.get("channel_uid"):
        m = re.search(r"channel_uid['\",:\s]+['\"]?([\d]+)", html)
        if m:
            params["channel_uid"] = m.group(1)
    m = re.search(r"merchant_name['\",:\s]+['\"]?([^'\"\),]+)", html)
    if m:
        params["merchant_name"] = m.group(1).strip()
    m = re.search(r"boi_uid['\",:\s]+['\"]?([\d]+)", html)
    if m:
        params["boi_uid"] = m.group(1)
    m = re.search(r"total_pay['\",:\s]+['\"]?([\d]+)", html)
    if m:
        params["total_pay"] = m.group(1)
    if not params.get("total_pay"):
        m = re.search(r'Amount to Pay.*?IDR\s*([\d.,]+)', html, re.DOTALL)
        if m:
            params["total_pay"] = m.group(1).replace(".", "").replace(",", "")
    m = re.search(r"bill_expired['\",:\s]+['\"]([^'\"]+)", html)
    if m:
        params["bill_expired"] = m.group(1).strip()
    m = re.search(r"customer['\",:\s]+['\"]([^'\"]+@[^'\"]+)", html)
    if m:
        params["customer"] = m.group(1).strip()
    m = re.search(r"color['\",:\s]+['\"]([a-fA-F0-9]{6})", html)
    if m:
        params["color"] = m.group(1)
    if params.get("channel_uid"):
        params["channel"] = params["channel_uid"]
    return params


def extract_order_no(html):
    match = re.search(r'Order No.*?<td[^>]*class="[^"]*text-end[^"]*"[^>]*>([^<]+)', html, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r'Nomor Pesanan.*?<td[^>]*>([^<]+)', html, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def extract_total_amount(html):
    match = re.search(r'Grand Total.*?<td[^>]*>([^<]+)', html, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r'summary-total["\'][^>]*>([^<]+)', html)
    if match:
        return match.group(1).strip()
    return ""


# =============================================================================
# REQUEST HELPER
# =============================================================================

def req(session, method, url, data=None, retry=MAX_RETRY, allow_redirects=True,
        headers_extra=None, is_json=False):
    for attempt in range(1, retry + 1):
        try:
            kw_headers = None
            if headers_extra:
                kw_headers = dict(session.headers)
                kw_headers.update(headers_extra)
            if method == "GET":
                r = session.get(url, timeout=30, allow_redirects=allow_redirects, headers=kw_headers)
            else:
                if is_json:
                    r = session.post(url, json=data, timeout=30, allow_redirects=allow_redirects, headers=kw_headers)
                else:
                    r = session.post(url, data=data, timeout=30, allow_redirects=allow_redirects, headers=kw_headers)
            return r
        except requests.exceptions.RequestException as e:
            if attempt < retry:
                time.sleep(0.3)
            else:
                safe_print(f"  [ERROR] Request gagal setelah {retry}x: {e}")
                return None


# =============================================================================
# PRINT SUMMARY
# =============================================================================

def print_summary(results_list, total_pembeli):
    print(f"\n\n{'='*60}")
    print(f"  RINGKASAN AKHIR")
    print(f"{'='*60}")
    for r in results_list:
        s = "OK" if r.get("ok") else "GAGAL"
        print(f"  [{s}] {r['nama']}")
        if r.get("ok"):
            print(f"        Order No : {r.get('order_no', '-')}")
            print(f"        Total    : {r.get('total', '-')}")
            print(f"        Channel  : {r.get('payment_channel', '-')}")
            if r.get("qr_url"):
                print(f"        QR URL   : {r['qr_url']}")
            if r.get("expired"):
                print(f"        Expired  : {r['expired']}")
            if r.get("payment_url"):
                print(f"        Pay URL  : {r['payment_url']}")
            print(f"        Email    : {'YA' if r.get('email_sent') else 'TIDAK'}")
        else:
            print(f"        Error    : {r.get('error', 'Unknown')}")
        print()
    ok = sum(1 for r in results_list if r.get("ok"))
    fail = len(results_list) - ok
    print(f"  {ok} berhasil, {fail} gagal dari {len(results_list)} pembeli")
    print(f"{'='*60}\n")



# =============================================================================
# MAIN PURCHASE FLOW
# =============================================================================

def run_purchase(event_url, pembeli, ticket_lokasi, pg_method, channel_code,
                 pembeli_num, total_pembeli):
    """Proses beli tiket untuk 1 pembeli. Retry sampai dapat."""
    prefix = f"  [{pembeli_num}/{total_pembeli}]"
    qty = pembeli.get("qty", 1)
    attempt = 0
    session = create_session()

    while not stop_flag.is_set():
        attempt += 1
        safe_print(f"\n{'='*60}")
        safe_print(f"{prefix} {pembeli['nama']} | Qty={qty} | Attempt #{attempt}")
        safe_print(f"{'='*60}")

        # STEP 1: GET event page
        safe_print(f"{prefix} [1/7] Ambil halaman event...", end=" ")
        r = req(session, "GET", event_url)
        if r is None or r.status_code != 200:
            safe_print(f"GAGAL ({r.status_code if r else 'N/A'})")
            continue
        safe_print("OK")

        csrf_token = extract_csrf_token(r.text)
        if not csrf_token:
            safe_print(f"{prefix} [!] CSRF token tidak ditemukan, retry...")
            continue

        # Hitung admin fee
        safe_print(f"{prefix} [1/7] Hitung admin fee...", end=" ")
        base_price = 0
        price_match = re.search(r'name="harga"[^>]*value="(\d+)"', r.text)
        if price_match:
            base_price = int(price_match.group(1))

        subtotal = base_price * qty
        admin_fee = 0
        if subtotal > 0 and pg_method:
            fee_r = req(session, "POST", f"{SITE_BASE}/api/adminfeeTicket",
                        data={"pg_code": pg_method, "harga": subtotal}, retry=2)
            if fee_r and fee_r.status_code == 200:
                try:
                    admin_fee = int(fee_r.text.strip())
                except (ValueError, TypeError):
                    admin_fee = 0
            safe_print(f"OK (fee={admin_fee})")
        else:
            safe_print("SKIP")

        total_amount = subtotal + admin_fee + 1000

        # STEP 2: POST form
        safe_print(f"{prefix} [2/7] Submit form...", end=" ")
        form_data = {
            "_token": csrf_token,
            "nama": pembeli["nama"],
            "no_identitas": pembeli["nik"],
            "email": pembeli["email"],
            "wa": pembeli["wa"],
            "lokasi": str(ticket_lokasi),
            "harga": str(base_price),
            "qty": str(qty),
            "pg_method": str(pg_method),
            "total": str(total_amount),
        }
        payment_url = event_url.rstrip("/") + "/payment"
        r = req(session, "POST", payment_url, data=form_data,
                headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                               "Referer": event_url, "Origin": SITE_BASE})
        if r is None or r.status_code not in (200, 301, 302, 303):
            safe_print(f"GAGAL ({r.status_code if r else 'N/A'})")
            continue
        safe_print(f"OK ({r.status_code})")
        current_url = r.url
        page_html = r.text

        # STEP 3: Faspay Summary
        safe_print(f"{prefix} [3/7] Faspay summary...", end=" ")
        order_no = ""
        if "faspay" in current_url.lower() or "ANT-FOMO" in page_html:
            order_no = extract_order_no(page_html)
            safe_print(f"OK (Order: {order_no})")

            wa_number = pembeli["wa"]
            if wa_number.startswith("0"):
                wa_number = wa_number[1:]
            elif wa_number.startswith("+62"):
                wa_number = wa_number[3:]
            elif wa_number.startswith("62"):
                wa_number = wa_number[2:]

            safe_print(f"{prefix} [3/7] Submit WA...", end=" ")
            r = req(session, "POST", f"{FASPAY_BASE}/payment/checkout",
                    data={"lang": "en", "tel": wa_number, "country_phone": "+62"},
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                                   "Referer": current_url})
            if r is None or r.status_code not in (200, 301, 302, 303):
                safe_print(f"GAGAL")
                continue
            safe_print("OK")
            current_url = r.url
            page_html = r.text
        else:
            safe_print("SKIP")

        # STEP 4: Faspay Checkout - pilih channel
        safe_print(f"{prefix} [4/7] Pilih channel {channel_code}...", end=" ")
        if "channel_list" in page_html or "Payment Method" in page_html:
            r = req(session, "POST", f"{FASPAY_BASE}/payment/order",
                    data={"pglist_rad": channel_code, "txtTerm": "on", "checkout": ""},
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                                   "Referer": current_url})
            if r is None or r.status_code not in (200, 301, 302, 303):
                safe_print("GAGAL")
                continue
            safe_print("OK")
            current_url = r.url
            page_html = r.text
        else:
            safe_print("SKIP")

        # STEP 5: Faspay Order confirm
        safe_print(f"{prefix} [5/7] Confirm payment...", end=" ")
        if "pglist_rad" in page_html and "checkout" in page_html:
            hidden = extract_hidden_fields(page_html)
            pay_data = {"pglist_rad": hidden.get("pglist_rad", channel_code),
                        "txtTerm": "on", "checkout": hidden.get("checkout", "1")}
            plan_match = re.search(r'name="payment_plan\[\]"[^>]*value="([^"]*)"', page_html)
            if plan_match:
                pay_data["payment_plan[]"] = plan_match.group(1)
            total_str = extract_total_amount(page_html)
            if not order_no:
                order_no = extract_order_no(page_html)
            safe_print(f"OK (Total: {total_str})")

            safe_print(f"{prefix} [5/7] Submit pay...", end=" ")
            r = req(session, "POST", f"{FASPAY_BASE}/payment/order",
                    data=pay_data,
                    headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                                   "Referer": current_url})
            if r is None or r.status_code not in (200, 301, 302, 303):
                safe_print("GAGAL")
                continue
            safe_print("OK")
            current_url = r.url
            page_html = r.text
        else:
            safe_print("SKIP")
            total_str = ""

        # STEP 6: Final payment page
        safe_print(f"{prefix} [6/7] Halaman pembayaran...", end=" ")
        payment_info = extract_qris_info(page_html)
        if not order_no:
            order_no = extract_order_no(page_html)
        if not total_str:
            total_str = payment_info.get("amount", extract_total_amount(page_html))

        channel_name = ""
        cn_match = re.search(r'alt="([^"]+)"', page_html)
        if cn_match:
            cn = cn_match.group(1)
            if "faspay" not in cn.lower() and "logo" not in cn.lower():
                channel_name = cn

        if payment_info.get("qr_url") or payment_info.get("trx_id") or "payment" in current_url.lower():
            safe_print("OK")
            safe_print(f"\n{prefix} {'='*45}")
            safe_print(f"{prefix} BERHASIL!")
            safe_print(f"{prefix} {'='*45}")
            safe_print(f"{prefix} Nama     : {pembeli['nama']}")
            safe_print(f"{prefix} Order    : {order_no}")
            safe_print(f"{prefix} Total    : {total_str}")
            safe_print(f"{prefix} Channel  : {channel_name}")
            if payment_info.get("qr_url"):
                safe_print(f"{prefix} QR URL   : {payment_info['qr_url']}")
            if payment_info.get("expired"):
                safe_print(f"{prefix} Expired  : {payment_info['expired']}")
            safe_print(f"{prefix} Page URL : {current_url}")
            safe_print(f"{prefix} {'='*45}")

            # STEP 7: Send email
            safe_print(f"{prefix} [7/7] Kirim email...", end=" ")
            email_params = extract_email_params(page_html)
            trx_uid = email_params.get("trx_uid", payment_info.get("trx_id", ""))
            email_sent = False
            if trx_uid:
                ch_uid = email_params.get("channel_uid", channel_code)
                merchant = email_params.get("merchant_name", "ANT-FOMO")
                email_data = {
                    "trx_uid": trx_uid,
                    "channel_uid": ch_uid,
                    "language": "en",
                    "merchant_name": merchant,
                    "total_pay": email_params.get("total_pay", ""),
                    "custEmail": pembeli["email"],
                    "bill_expired": email_params.get("bill_expired", ""),
                    "channel": ch_uid,
                    "customer": email_params.get("customer", pembeli["email"]),
                    "boi": merchant,
                    "boi_uid": email_params.get("boi_uid", ""),
                    "color": email_params.get("color", "fd9c41"),
                }
                er = req(session, "POST", f"{FASPAY_BASE}/payment/sendemailpdf",
                         data=email_data,
                         headers_extra={"Content-Type": "application/x-www-form-urlencoded",
                                        "Referer": current_url}, retry=3)
                if er and er.status_code == 200:
                    safe_print(f"OK ({pembeli['email']})")
                    email_sent = True
                else:
                    safe_print("GAGAL")
            else:
                safe_print("SKIP (no trx_uid)")

            return {
                "nama": pembeli["nama"], "ok": True, "email": pembeli["email"],
                "order_no": order_no, "total": total_str,
                "payment_channel": channel_name,
                "qr_url": payment_info.get("qr_url", ""),
                "expired": payment_info.get("expired", ""),
                "trx_id": payment_info.get("trx_id", ""),
                "payment_url": current_url, "email_sent": email_sent,
            }
        else:
            safe_print(f"GAGAL (url: {current_url[:60]})")
            continue

    return {"nama": pembeli["nama"], "ok": False, "error": "Dihentikan paksa"}


# =============================================================================
# MAIN - TANPA INPUT, LANGSUNG GAS
# =============================================================================

def main():
    print("\n" + "=" * 60)
    print("  BOT TIKET EXPLOREFOMO")
    print("  Tanpa pertanyaan - langsung gas dari config.txt")
    print("  Ctrl+C = stop paksa + ringkasan")
    print("=" * 60)

    # Load config
    config = load_config()
    event_url = config.get("EVENT_URL", f"{SITE_BASE}/VikingFest2026")
    ticket_lokasi = int(config.get("LOKASI", "1"))
    pg_method = config.get("PG_METHOD", "711")
    channel_code = pg_method
    war_time_str = config.get("WAR_TIME", "")
    mode = config.get("MODE", "parallel").lower()
    akun_choice = config.get("AKUN", "all")

    if not event_url.startswith("http"):
        event_url = f"{SITE_BASE}/{event_url}"

    # Load data pembeli
    all_pembeli = load_data_pembeli()
    selected_pembeli = select_pembeli(all_pembeli, akun_choice)

    if not selected_pembeli:
        print("  [!] Tidak ada akun dipilih")
        return

    parallel = mode != "sequential"

    # Print config
    print(f"\n  {'='*55}")
    print(f"  CONFIG LOADED - LANGSUNG GAS")
    print(f"  {'='*55}")
    print(f"  Event    : {event_url}")
    print(f"  Lokasi   : {ticket_lokasi}")
    print(f"  Payment  : {pg_method}")
    print(f"  Mode     : {'PARALEL' if parallel else 'SEQUENTIAL'}")
    print(f"  Akun     : {len(selected_pembeli)} orang")
    for p in selected_pembeli:
        print(f"             - {p['nama']} (qty={p['qty']})")
    if war_time_str:
        print(f"  War Time : {war_time_str} WIB")
    else:
        print(f"  War Time : LANGSUNG GAS")
    print(f"  {'='*55}")

    # Countdown war
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
            if diff <= 0:
                break
            h = int(diff // 3600)
            m = int((diff % 3600) // 60)
            s = int(diff % 60)
            sys.stdout.write(f"\r  \u23F1  {h:02d}:{m:02d}:{s:02d} ... ")
            sys.stdout.flush()
            time.sleep(0.5)
        print(f"\n\n  WAKTU HABIS! GAS!!!\n")

    print(f"\n  >> MULAI BELI TIKET...\n")

    # Signal handler
    stop_flag.clear()
    results.clear()

    def signal_handler(sig, frame):
        stop_flag.set()
        raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGINT, signal_handler)

    # Execute
    executor = None

    def on_done(future, p_info):
        try:
            result = future.result()
        except Exception as e:
            result = {"nama": p_info["nama"], "ok": False, "error": str(e)}
        with results_lock:
            results.append(result)

    try:
        if parallel and len(selected_pembeli) > 1:
            executor = ThreadPoolExecutor(max_workers=len(selected_pembeli))
            futures = {}
            for i, pembeli in enumerate(selected_pembeli, 1):
                f = executor.submit(run_purchase, event_url, pembeli, ticket_lokasi,
                                    pg_method, channel_code, i, len(selected_pembeli))
                f.add_done_callback(lambda fut, p=pembeli: on_done(fut, p))
                futures[f] = pembeli
            for f in as_completed(futures):
                pass
        else:
            for i, pembeli in enumerate(selected_pembeli, 1):
                if stop_flag.is_set():
                    with results_lock:
                        results.append({"nama": pembeli["nama"], "ok": False, "error": "Dihentikan paksa"})
                    continue
                result = run_purchase(event_url, pembeli, ticket_lokasi,
                                      pg_method, channel_code, i, len(selected_pembeli))
                with results_lock:
                    results.append(result)

    except KeyboardInterrupt:
        safe_print(f"\n\n  [!] STOP PAKSA...")
        stop_flag.set()
    finally:
        signal.signal(signal.SIGINT, old_handler)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.5)
        with results_lock:
            done_names = {r["nama"] for r in results}
            for p in selected_pembeli:
                if p["nama"] not in done_names:
                    results.append({"nama": p["nama"], "ok": False, "error": "Dihentikan paksa"})
        print_summary(results, len(selected_pembeli))


if __name__ == "__main__":
    main()
