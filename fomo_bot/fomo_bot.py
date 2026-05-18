#!/usr/bin/env python3
"""
Bot Tiket ExploreFomo (Hybrid: API + Playwright)
- API (requests) untuk form submit explorefomo + Faspay POST endpoints
- Playwright HANYA untuk render halaman Faspay yang pakai JS (xpress.payment.js)
- Pilihan akun di awal
- Paralel otomatis (asyncio + aiohttp untuk API, Playwright untuk JS pages)
- Retry tanpa jeda
- Ctrl+C langsung ringkasan

Cara pakai:
    1. Edit data_pembeli.txt
    2. pip install aiohttp playwright && playwright install chromium
    3. python fomo_bot.py
"""

import os
import re
import sys
import asyncio
import signal
from datetime import datetime, timedelta, timezone

try:
    import aiohttp
except ImportError:
    print("\n  [ERROR] aiohttp belum terinstall! -> pip install aiohttp")
    sys.exit(1)

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("\n  [ERROR] Playwright belum terinstall!")
    print("  Jalankan: pip install playwright && playwright install chromium")
    sys.exit(1)

WIB = timezone(timedelta(hours=7))

# =============================================================================
SITE_BASE = "https://sites.explorefomo.id"
FASPAY_BASE = "https://xpress.faspay.co.id/v4"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_RETRY = 5
PAGE_TIMEOUT = 30000  # 30s for Playwright

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "data_pembeli.txt")

stop_flag = False
results = []
print_lock = asyncio.Lock()


async def safe_print(*args, **kwargs):
    async with print_lock:
        print(*args, **kwargs)
        sys.stdout.flush()


# =============================================================================
# DATA LOADER
# =============================================================================

def load_data_pembeli():
    if not os.path.exists(DATA_FILE):
        print(f"\n  [ERROR] File tidak ditemukan: {DATA_FILE}")
        print(f"  Format: NAMA|NIK|EMAIL|NO_WA|QTY|LOKASI")
        sys.exit(1)

    data = []
    with open(DATA_FILE, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                print(f"  [WARNING] Baris {line_num} format salah: {line[:80]}")
                continue

            qty = 1
            if len(parts) >= 5 and parts[4].strip().isdigit():
                qty = max(1, min(3, int(parts[4].strip())))
            lokasi = int(parts[5].strip()) if len(parts) > 5 and parts[5].strip().isdigit() else 1

            data.append({
                "nama": parts[0].strip(),
                "nik": parts[1].strip(),
                "email": parts[2].strip(),
                "wa": parts[3].strip(),
                "qty": qty,
                "lokasi": lokasi,
            })

    if not data:
        print(f"\n  [ERROR] Tidak ada data valid di {DATA_FILE}")
        sys.exit(1)
    return data


# =============================================================================
# ACCOUNT SELECTION
# =============================================================================

def select_accounts(all_pembeli):
    print(f"\n  {'='*50}")
    print(f"  PILIH AKUN YANG AKAN DIJALANKAN")
    print(f"  {'='*50}")
    print(f"  0. SEMUA AKUN ({len(all_pembeli)} akun)")
    for i, p in enumerate(all_pembeli, 1):
        print(f"  {i}. {p['nama']} | {p['email']} | qty={p['qty']}")
    print(f"  {'='*50}")
    print(f"  Masukkan nomor (pisah koma). Contoh: 1,3 atau 0 untuk semua")

    while True:
        choice = input("  > ").strip()
        if not choice:
            print("  [!] Pilih minimal 1 akun.")
            continue
        if choice == "0":
            print(f"  >> Semua {len(all_pembeli)} akun dipilih.")
            return all_pembeli
        try:
            indices = [int(x.strip()) for x in choice.split(",")]
            selected = []
            for idx in indices:
                if 1 <= idx <= len(all_pembeli):
                    selected.append(all_pembeli[idx - 1])
                else:
                    print(f"  [!] Nomor {idx} tidak valid")
                    selected = []
                    break
            if selected:
                seen = set()
                unique = []
                for s in selected:
                    if s["nama"] not in seen:
                        seen.add(s["nama"])
                        unique.append(s)
                print(f"  >> {len(unique)} akun: {', '.join(s['nama'] for s in unique)}")
                return unique
        except ValueError:
            print("  [!] Format salah, pakai angka pisah koma")


# =============================================================================
# HTML HELPERS
# =============================================================================

def extract_csrf(html):
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    if m: return m.group(1)
    m = re.search(r'value="([^"]+)"\s+name="_token"', html)
    if m: return m.group(1)
    return None


def extract_order_no(html):
    m = re.search(r'Order No.*?<td[^>]*>([^<]+)', html, re.DOTALL)
    if m: return m.group(1).strip()
    m = re.search(r'ANT-FOMO[\w-]+', html)
    if m: return m.group(0)
    return ""


# =============================================================================
# PURCHASE FLOW (HYBRID: API + Playwright)
# =============================================================================

async def run_purchase(pembeli, pembeli_num, total, event_url, pw_instance):
    """
    Strategy:
    1. API: GET event page -> extract CSRF + harga
    2. API: POST /payment -> get redirect to Faspay
    3. Playwright: open Faspay URL -> wait for JS render -> interact with payment
    """
    global stop_flag
    prefix = f"  [{pembeli_num}/{total}]"
    lokasi = pembeli["lokasi"]
    qty = pembeli["qty"]
    pg_method = pembeli["pg_method"]
    attempt = 0

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id,en-US;q=0.7,en;q=0.3",
        "Connection": "keep-alive",
    }

    browser = None

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            while not stop_flag:
                attempt += 1

                await safe_print(f"\n{'='*60}")
                await safe_print(f"{prefix} {pembeli['nama']} | Qty={qty} | Attempt #{attempt}")
                await safe_print(f"{'='*60}")

                # ─── STEP 1: API - GET event page ───
                await safe_print(f"{prefix} [1/6] GET event (API)...", end=" ")
                try:
                    async with session.get(event_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            await safe_print(f"GAGAL ({resp.status})")
                            continue
                        html = await resp.text()
                except Exception as e:
                    await safe_print(f"ERROR: {str(e)[:60]}")
                    continue
                await safe_print("OK")

                csrf = extract_csrf(html)
                if not csrf:
                    await safe_print(f"{prefix} [!] No CSRF token, retry...")
                    continue

                # Extract base price
                base_price = 0
                pm = re.search(r'name="harga"[^>]*value="(\d+)"', html)
                if pm:
                    base_price = int(pm.group(1))

                # ─── STEP 2: API - GET admin fee ───
                subtotal = base_price * qty
                admin_fee = 0
                if subtotal > 0:
                    try:
                        async with session.post(
                            f"{SITE_BASE}/api/adminfeeTicket",
                            data={"pg_code": pg_method, "harga": str(subtotal)},
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status == 200:
                                txt = await resp.text()
                                admin_fee = int(txt.strip())
                    except Exception:
                        pass
                total_amount = subtotal + admin_fee + 1000

                # ─── STEP 3: API - POST payment form ───
                await safe_print(f"{prefix} [2/6] POST payment (API)...", end=" ")
                form_data = {
                    "_token": csrf,
                    "nama": pembeli["nama"],
                    "no_identitas": pembeli["nik"],
                    "email": pembeli["email"],
                    "wa": pembeli["wa"],
                    "lokasi": str(lokasi),
                    "harga": str(base_price),
                    "qty": str(qty),
                    "pg_method": str(pg_method),
                    "total": str(total_amount),
                }
                payment_url = event_url.rstrip("/") + "/payment"

                faspay_url = None
                try:
                    async with session.post(
                        payment_url,
                        data=form_data,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": event_url,
                            "Origin": SITE_BASE,
                        },
                        allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        final_url = str(resp.url)
                        resp_html = await resp.text()

                        if "faspay" in final_url.lower():
                            faspay_url = final_url
                            await safe_print(f"OK -> Faspay redirect")
                        elif resp.status in (200, 301, 302, 303):
                            # Check if body has faspay redirect/meta refresh
                            m = re.search(r'(https?://[^"\']+faspay[^"\']*)', resp_html)
                            if m:
                                faspay_url = m.group(1)
                                await safe_print(f"OK -> Faspay URL extracted")
                            else:
                                await safe_print(f"OK ({resp.status}) - no Faspay redirect?")
                                # Maybe payment already done on explorefomo side
                                faspay_url = final_url
                        else:
                            await safe_print(f"GAGAL ({resp.status})")
                            continue
                except Exception as e:
                    await safe_print(f"ERROR: {str(e)[:60]}")
                    continue

                if not faspay_url:
                    await safe_print(f"{prefix} [!] No Faspay URL, retry...")
                    continue

                # ─── STEP 4: PLAYWRIGHT - Render Faspay JS page ───
                await safe_print(f"{prefix} [3/6] Playwright: open Faspay...", end=" ")

                if not browser:
                    browser = await pw_instance.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage"]
                    )

                context = await browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1280, "height": 720},
                    locale="id-ID",
                )
                context.set_default_timeout(PAGE_TIMEOUT)

                # Transfer cookies from aiohttp session to Playwright
                cookies_to_set = []
                for cookie in session.cookie_jar:
                    cookies_to_set.append({
                        "name": cookie.key,
                        "value": cookie.value,
                        "domain": cookie["domain"] or ".faspay.co.id",
                        "path": cookie["path"] or "/",
                    })
                if cookies_to_set:
                    await context.add_cookies(cookies_to_set)

                page = await context.new_page()
                try:
                    try:
                        await page.goto(faspay_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
                    except PWTimeout:
                        await page.goto(faspay_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

                    # Wait for xpress.payment.js to render
                    try:
                        await page.wait_for_selector(
                            ".channel_list, #channel_list, .payment-channel, "
                            "input[name='tel'], .qris-card-qr, text=Order No, "
                            "text=Payment Method, text=Amount to Pay",
                            timeout=15000
                        )
                    except PWTimeout:
                        pass  # proceed anyway

                    await safe_print("OK (JS rendered)")

                    # ─── STEP 5: Playwright - Handle Faspay flow ───
                    await safe_print(f"{prefix} [4/6] Faspay flow...", end=" ")
                    html = await page.content()
                    current_url = page.url

                    # WA number input
                    if "tel" in html.lower() and "country_phone" in html.lower():
                        wa = pembeli["wa"]
                        if wa.startswith("0"):
                            wa = wa[1:]
                        elif wa.startswith("+62"):
                            wa = wa[3:]
                        elif wa.startswith("62"):
                            wa = wa[2:]

                        try:
                            tel_input = await page.query_selector('input[name="tel"], input#tel')
                            if tel_input:
                                await tel_input.fill(wa)
                            submit_btn = await page.query_selector(
                                'button[type="submit"], input[type="submit"], .btn-checkout, #btn-checkout'
                            )
                            if submit_btn:
                                async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                                    await submit_btn.click()
                        except (PWTimeout, Exception):
                            pass
                        html = await page.content()
                        current_url = page.url

                    # Channel selection
                    if "channel_list" in html.lower() or "payment method" in html.lower():
                        try:
                            # Select channel radio
                            channel_el = await page.query_selector(
                                f'input[value="{pg_method}"], [data-channel="{pg_method}"]'
                            )
                            if channel_el:
                                await channel_el.click()
                                await asyncio.sleep(0.3)

                            # Terms checkbox
                            terms = await page.query_selector('input[name="txtTerm"], input#txtTerm')
                            if terms:
                                await terms.check()

                            # Checkout button
                            pay_btn = await page.query_selector(
                                'button[name="checkout"], input[name="checkout"], '
                                '.btn-pay, #btn-pay, button.checkout'
                            )
                            if pay_btn:
                                async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                                    await pay_btn.click()
                        except (PWTimeout, Exception):
                            pass
                        html = await page.content()
                        current_url = page.url

                    # Confirm page
                    if "confirm" in html.lower() or "pglist_rad" in html.lower():
                        try:
                            confirm_btn = await page.query_selector(
                                'button[type="submit"], .btn-confirm, #btn-confirm'
                            )
                            if confirm_btn:
                                async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                                    await confirm_btn.click()
                        except (PWTimeout, Exception):
                            pass
                        html = await page.content()
                        current_url = page.url

                    await safe_print("OK")

                    # ─── STEP 6: Extract result ───
                    await safe_print(f"{prefix} [5/6] Extract result...", end=" ")

                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PWTimeout:
                        pass

                    html = await page.content()
                    current_url = page.url

                    order_no = await page.evaluate('''() => {
                        const tds = document.querySelectorAll('td');
                        for(let td of tds) {
                            if(td.textContent.includes('ANT-FOMO')) return td.textContent.trim();
                        }
                        const m = document.body.innerHTML.match(/ANT-FOMO[\\w-]+/);
                        return m ? m[0] : '';
                    }''')

                    total_str = await page.evaluate('''() => {
                        const el = document.querySelector('.total-amount, #total-amount, .grand-total');
                        if (el) return el.textContent.trim();
                        const tds = document.querySelectorAll('td');
                        for(let td of tds) {
                            if(td.textContent.includes('IDR')) return td.textContent.trim();
                        }
                        return '';
                    }''')

                    qr_url = await page.evaluate('''() => {
                        const img = document.querySelector(
                            'img.qris-card-qr, img[class*="qr"], img[alt*="qr"], img[src*="qr"]'
                        );
                        return img ? img.src : '';
                    }''')

                    expired = await page.evaluate('''() => {
                        const m = document.body.innerHTML.match(
                            /Expired?.*?(\\d{2}\\/\\d{2}\\/\\d{4}[^<]*)/i
                        );
                        return m ? m[1].trim() : '';
                    }''')

                    is_payment_page = bool(
                        qr_url or order_no or
                        "payment" in current_url.lower() or
                        "qris" in html.lower() or
                        "Amount to Pay" in html
                    )

                    if is_payment_page:
                        await safe_print("BERHASIL!")
                        await safe_print(f"{prefix} Order  : {order_no or '-'}")
                        await safe_print(f"{prefix} Total  : {total_str or '-'}")
                        if qr_url:
                            await safe_print(f"{prefix} QR     : {qr_url}")
                        if expired:
                            await safe_print(f"{prefix} Exp    : {expired}")
                        await safe_print(f"{prefix} URL    : {current_url}")

                        # Send email via page context
                        await safe_print(f"{prefix} [6/6] Email...", end=" ")
                        email_sent = False
                        try:
                            email_sent = await page.evaluate(f'''async () => {{
                                try {{
                                    const resp = await fetch('/v4/payment/sendemailpdf', {{
                                        method: 'POST',
                                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                                        body: new URLSearchParams({{
                                            custEmail: '{pembeli["email"]}',
                                            language: 'en'
                                        }})
                                    }});
                                    return resp.ok;
                                }} catch(e) {{ return false; }}
                            }}''')
                        except Exception:
                            pass
                        await safe_print("OK" if email_sent else "SKIP")

                        await page.close()
                        await context.close()
                        return {
                            "nama": pembeli["nama"], "ok": True,
                            "email": pembeli["email"],
                            "order_no": order_no, "total": total_str,
                            "qr_url": qr_url, "expired": expired,
                            "payment_url": current_url,
                            "email_sent": email_sent,
                        }
                    else:
                        await safe_print("GAGAL - belum payment page")
                        debug_path = os.path.join(SCRIPT_DIR, f"debug_{pembeli_num}_{attempt}.png")
                        try:
                            await page.screenshot(path=debug_path)
                            await safe_print(f"{prefix} Screenshot: {debug_path}")
                        except Exception:
                            pass
                        await page.close()
                        await context.close()
                        continue

                except Exception as e:
                    await safe_print(f"ERROR: {str(e)[:80]}")
                    try:
                        await page.close()
                        await context.close()
                    except Exception:
                        pass
                    if attempt >= MAX_RETRY:
                        return {"nama": pembeli["nama"], "ok": False, "error": str(e)[:200]}
                    continue

    finally:
        if browser:
            await browser.close()

    return {"nama": pembeli["nama"], "ok": False, "error": "Dihentikan paksa"}


# =============================================================================
# SUMMARY
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
            if r.get("qr_url"):
                print(f"        QR     : {r['qr_url']}")
            if r.get("expired"):
                print(f"        Exp    : {r['expired']}")
            if r.get("payment_url"):
                print(f"        URL    : {r['payment_url']}")
            print(f"        Email  : {'YA' if r.get('email_sent') else 'TIDAK'}")
        else:
            print(f"        Error  : {r.get('error', '?')}")
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n  {ok} berhasil, {len(results)-ok} gagal dari {len(results)} pembeli")
    print(f"{'='*60}\n")


# =============================================================================
# MAIN
# =============================================================================

async def async_main():
    global stop_flag

    print("\n" + "=" * 60)
    print("  BOT TIKET EXPLOREFOMO (Hybrid: API + Playwright)")
    print("  API untuk form submit, Playwright untuk Faspay JS render")
    print("  Ctrl+C = stop + ringkasan")
    print("=" * 60)

    all_pembeli = load_data_pembeli()
    print(f"\n  Loaded: {len(all_pembeli)} akun dari data_pembeli.txt")

    # PILIHAN AKUN
    selected = select_accounts(all_pembeli)

    # CLI INPUT
    print(f"\n  Paste link event:")
    event_url = input("  > ").strip()
    if not event_url:
        print("  [!] Event URL kosong, exit.")
        return
    if not event_url.startswith("http"):
        event_url = f"{SITE_BASE}/{event_url}"

    print(f"\n  Metode pembayaran (711=QRIS, 802=VA BCA, 800=VA Mandiri) [default: 711]:")
    pg_method = input("  > ").strip() or "711"

    print(f"\n  Jam war WIB (HH:MM:SS) [kosong = langsung gas]:")
    war_time_str = input("  > ").strip()

    for p in selected:
        p["pg_method"] = pg_method

    print(f"\n  {'─'*50}")
    print(f"  Event   : {event_url}")
    print(f"  Payment : {pg_method}")
    print(f"  Akun    : {len(selected)} dipilih")
    print(f"  War     : {war_time_str or 'LANGSUNG GAS'}")
    print(f"  {'─'*50}")

    # Countdown
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
            await asyncio.sleep(0.5)
        print(f"\n\n  GAS!!!\n")
    else:
        print(f"\n  War Time: LANGSUNG GAS\n")

    print(f"  >> MULAI (API + Playwright hybrid)...\n")

    stop_flag = False
    results.clear()

    async with async_playwright() as pw:
        tasks = []
        for i, p in enumerate(selected, 1):
            task = asyncio.create_task(
                run_purchase(p, i, len(selected), event_url, pw)
            )
            tasks.append(task)

        try:
            done_results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in done_results:
                if isinstance(r, Exception):
                    results.append({"nama": "?", "ok": False, "error": str(r)[:200]})
                elif isinstance(r, dict):
                    results.append(r)
        except KeyboardInterrupt:
            print(f"\n\n  [!] STOP PAKSA...")
            stop_flag = True
            for t in tasks:
                t.cancel()
            await asyncio.sleep(1)

    done_names = {r["nama"] for r in results}
    for p in selected:
        if p["nama"] not in done_names:
            results.append({"nama": p["nama"], "ok": False, "error": "Dihentikan paksa"})

    print_summary()


def main():
    global stop_flag

    def sig_handler(sig, frame):
        global stop_flag
        stop_flag = True
        print(f"\n\n  [!] STOP PAKSA (Ctrl+C)...")

    signal.signal(signal.SIGINT, sig_handler)

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        stop_flag = True
        print_summary()


if __name__ == "__main__":
    main()
