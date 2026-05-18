#!/usr/bin/env python3
"""
Bot Tiket ExploreFomo (Playwright Edition)
- Menggunakan Playwright untuk handle Faspay JS-rendered pages
- Pilihan akun di awal (bisa pilih semua atau sebagian)
- Event URL ditanya 1x di CLI
- Data pembeli dari data_pembeli.txt
- Paralel otomatis (multi-context browser)
- Retry tanpa jeda
- Ctrl+C langsung ringkasan

Format data_pembeli.txt:
    NAMA|NIK|EMAIL|NO_WA|QTY|LOKASI|PG_METHOD|WAR_TIME

Cara pakai:
    1. Edit data_pembeli.txt
    2. pip install playwright && playwright install chromium
    3. python fomo_bot.py
    4. Pilih akun -> Paste event URL -> Enter -> gas
"""

import os
import re
import sys
import asyncio
import signal
from datetime import datetime, timedelta, timezone

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
PAGE_TIMEOUT = 30000  # 30 detik

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
    """Load dari data_pembeli.txt
    Format: NAMA|NIK|EMAIL|NO_WA|QTY|LOKASI
    """
    if not os.path.exists(DATA_FILE):
        print(f"\n  [ERROR] File tidak ditemukan: {DATA_FILE}")
        print(f"  Format: NAMA|NIK|EMAIL|NO_WA|QTY|LOKASI")
        print(f"  Contoh: BUDI|330123|budi@mail.com|08123|1|1")
        sys.exit(1)

    data = []
    with open(DATA_FILE, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                print(f"  [WARNING] Baris {line_num} kurang field (min 4): {line[:80]}")
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
        print(f"\n  [ERROR] Tidak ada data di {DATA_FILE}")
        sys.exit(1)
    return data


# =============================================================================
# ACCOUNT SELECTION
# =============================================================================

def select_accounts(all_pembeli):
    """Menu pilihan akun - user bisa pilih semua atau sebagian."""
    print(f"\n  {'='*50}")
    print(f"  PILIH AKUN YANG AKAN DIJALANKAN")
    print(f"  {'='*50}")
    print(f"  0. SEMUA AKUN ({len(all_pembeli)} akun)")
    for i, p in enumerate(all_pembeli, 1):
        print(f"  {i}. {p['nama']} | {p['email']} | qty={p['qty']}")
    print(f"  {'='*50}")
    print(f"  Masukkan nomor akun (pisah koma untuk multiple)")
    print(f"  Contoh: 1,3 atau 0 untuk semua")

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
                    print(f"  [!] Nomor {idx} tidak valid (1-{len(all_pembeli)})")
                    selected = []
                    break
            if selected:
                # Remove duplicates while preserving order
                seen = set()
                unique = []
                for s in selected:
                    if s["nama"] not in seen:
                        seen.add(s["nama"])
                        unique.append(s)
                print(f"  >> {len(unique)} akun dipilih: {', '.join(s['nama'] for s in unique)}")
                return unique
        except ValueError:
            print("  [!] Format salah. Gunakan angka dipisah koma (misal: 1,2,3)")


# =============================================================================
# PLAYWRIGHT PURCHASE FLOW
# =============================================================================

async def run_purchase(pembeli, pembeli_num, total, event_url, playwright_instance):
    """Beli tiket 1 pembeli menggunakan Playwright. Retry sampai dapat atau di-stop."""
    global stop_flag
    prefix = f"  [{pembeli_num}/{total}]"
    lokasi = pembeli["lokasi"]
    qty = pembeli["qty"]
    pg_method = pembeli["pg_method"]
    attempt = 0

    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )

    try:
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 720},
            locale="id-ID",
        )
        context.set_default_timeout(PAGE_TIMEOUT)

        while not stop_flag:
            attempt += 1
            page = await context.new_page()

            try:
                await safe_print(f"\n{'='*60}")
                await safe_print(f"{prefix} {pembeli['nama']} | Qty={qty} | Attempt #{attempt}")
                await safe_print(f"{'='*60}")

                # STEP 1: GET event page
                await safe_print(f"{prefix} [1/7] GET event page...", end=" ")
                try:
                    await page.goto(event_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
                except PWTimeout:
                    await page.goto(event_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                await safe_print("OK")

                # Extract CSRF
                csrf = await page.evaluate('''() => {
                    const el = document.querySelector('input[name="_token"]');
                    return el ? el.value : null;
                }''')
                if not csrf:
                    await safe_print(f"{prefix} [!] No CSRF token, retry...")
                    await page.close()
                    continue

                # Get base price
                base_price = await page.evaluate('''() => {
                    const el = document.querySelector('input[name="harga"]');
                    return el ? parseInt(el.value) || 0 : 0;
                }''')

                # STEP 2: Fill & submit form
                await safe_print(f"{prefix} [2/7] Fill form...", end=" ")

                # Fill form fields
                await page.evaluate(f'''() => {{
                    const setVal = (name, val) => {{
                        const el = document.querySelector('input[name="' + name + '"], select[name="' + name + '"]');
                        if (el) {{
                            el.value = val;
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        }}
                    }};
                    setVal('nama', '{pembeli["nama"]}');
                    setVal('no_identitas', '{pembeli["nik"]}');
                    setVal('email', '{pembeli["email"]}');
                    setVal('wa', '{pembeli["wa"]}');
                    setVal('lokasi', '{lokasi}');
                    setVal('qty', '{qty}');
                    setVal('pg_method', '{pg_method}');
                }}''')
                await safe_print("OK")

                # Submit form via navigation
                await safe_print(f"{prefix} [3/7] Submit payment...", end=" ")
                subtotal = base_price * qty
                admin_fee = 0
                total_amount = subtotal + admin_fee + 1000

                # Try submitting the form
                payment_url = event_url.rstrip("/") + "/payment"
                try:
                    await page.evaluate(f'''() => {{
                        const form = document.querySelector('form');
                        if (form) {{
                            // Ensure hidden fields
                            const setHidden = (name, val) => {{
                                let el = form.querySelector('input[name="' + name + '"]');
                                if (!el) {{
                                    el = document.createElement('input');
                                    el.type = 'hidden';
                                    el.name = name;
                                    form.appendChild(el);
                                }}
                                el.value = val;
                            }};
                            setHidden('_token', '{csrf}');
                            setHidden('nama', '{pembeli["nama"]}');
                            setHidden('no_identitas', '{pembeli["nik"]}');
                            setHidden('email', '{pembeli["email"]}');
                            setHidden('wa', '{pembeli["wa"]}');
                            setHidden('lokasi', '{lokasi}');
                            setHidden('harga', '{base_price}');
                            setHidden('qty', '{qty}');
                            setHidden('pg_method', '{pg_method}');
                            setHidden('total', '{total_amount}');
                            form.action = '{payment_url}';
                            form.method = 'POST';
                        }}
                    }}''')

                    async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                        await page.evaluate("() => { document.querySelector('form').submit(); }")
                except PWTimeout:
                    # If networkidle times out, just wait for domcontentloaded
                    pass
                except Exception:
                    # Fallback: direct POST via page.goto
                    await page.goto(payment_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

                await safe_print("OK")
                current_url = page.url

                # STEP 4: Wait for Faspay JS to render
                await safe_print(f"{prefix} [4/7] Waiting for Faspay JS render...", end=" ")
                if "faspay" in current_url.lower():
                    # Wait for JS rendering (xpress.payment.js)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except PWTimeout:
                        pass

                    # Wait for dynamic content to appear
                    try:
                        await page.wait_for_selector(
                            "text=Payment Method, .channel_list, .payment-channel, #channel_list, .qris-card-qr, text=Order No",
                            timeout=10000
                        )
                    except PWTimeout:
                        pass
                    await safe_print("OK")
                else:
                    await safe_print("SKIP (not faspay)")

                # Get rendered HTML (after JS execution)
                html = await page.content()
                current_url = page.url

                # STEP 5: Handle Faspay checkout flow
                await safe_print(f"{prefix} [5/7] Faspay checkout...", end=" ")

                # WA number submit if needed
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
                        # Submit WA form
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
                    # Try to select payment channel via radio button or click
                    try:
                        channel_selector = f'input[value="{pg_method}"], [data-channel="{pg_method}"]'
                        channel_el = await page.query_selector(channel_selector)
                        if channel_el:
                            await channel_el.click()
                            await asyncio.sleep(0.5)

                        # Check terms checkbox
                        terms_cb = await page.query_selector('input[name="txtTerm"], input#txtTerm, .term-checkbox')
                        if terms_cb:
                            await terms_cb.check()

                        # Click checkout/pay button
                        pay_btn = await page.query_selector(
                            'button[name="checkout"], input[name="checkout"], .btn-pay, #btn-pay, button.checkout'
                        )
                        if pay_btn:
                            async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                                await pay_btn.click()
                    except (PWTimeout, Exception):
                        pass

                    html = await page.content()
                    current_url = page.url
                    await safe_print("OK")
                else:
                    await safe_print("SKIP")

                # STEP 6: Confirm payment if needed
                await safe_print(f"{prefix} [6/7] Confirm payment...", end=" ")
                if "confirm" in html.lower() or "pglist_rad" in html.lower():
                    try:
                        confirm_btn = await page.query_selector(
                            'button[type="submit"], .btn-confirm, #btn-confirm, button.pay-now'
                        )
                        if confirm_btn:
                            async with page.expect_navigation(wait_until="networkidle", timeout=PAGE_TIMEOUT):
                                await confirm_btn.click()
                    except (PWTimeout, Exception):
                        pass

                    html = await page.content()
                    current_url = page.url
                    await safe_print("OK")
                else:
                    await safe_print("SKIP")

                # STEP 7: Extract payment result
                await safe_print(f"{prefix} [7/7] Extract result...", end=" ")

                # Wait for final payment page to fully render
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass

                html = await page.content()
                current_url = page.url

                # Extract order info from rendered page
                order_no = await page.evaluate('''() => {
                    // Try various selectors for order number
                    const selectors = [
                        () => { const el = document.querySelector('.order-no, #order-no'); return el?.textContent; },
                        () => { const tds = document.querySelectorAll('td'); for(let td of tds) { if(td.textContent.includes('ANT-FOMO')) return td.textContent.trim(); } return null; },
                        () => { const m = document.body.innerHTML.match(/ANT-FOMO[\\w-]+/); return m ? m[0] : null; },
                    ];
                    for (const s of selectors) { const r = s(); if (r) return r.trim(); }
                    return '';
                }''')

                total_str = await page.evaluate('''() => {
                    const selectors = [
                        () => { const el = document.querySelector('.total-amount, #total-amount, .grand-total'); return el?.textContent; },
                        () => { const tds = document.querySelectorAll('td'); for(let td of tds) { if(td.textContent.includes('IDR') || td.textContent.match(/[\\d.,]+/)) { const m = td.textContent.match(/[\\d.,]+/); if(m && m[0].length > 3) return td.textContent.trim(); }} return null; },
                    ];
                    for (const s of selectors) { const r = s(); if (r) return r.trim(); }
                    return '';
                }''')

                qr_url = await page.evaluate('''() => {
                    const img = document.querySelector('img.qris-card-qr, img[class*="qr"], img[alt*="qr"], img[src*="qr"]');
                    return img ? img.src : '';
                }''')

                expired = await page.evaluate('''() => {
                    const el = document.querySelector('.expired, .exp-date, [class*="expir"]');
                    if (el) return el.textContent.trim();
                    const m = document.body.innerHTML.match(/Expired?.*?(\\d{2}\\/\\d{2}\\/\\d{4}[^<]*)/i);
                    return m ? m[1].trim() : '';
                }''')

                # Check if we got payment page
                is_payment_page = bool(
                    qr_url or
                    order_no or
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

                    # Try send email via JS on page
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

                    if email_sent:
                        await safe_print(f"{prefix} Email  : SENT")
                    else:
                        await safe_print(f"{prefix} Email  : SKIP")

                    await page.close()
                    return {
                        "nama": pembeli["nama"], "ok": True, "email": pembeli["email"],
                        "order_no": order_no, "total": total_str,
                        "qr_url": qr_url, "expired": expired,
                        "payment_url": current_url, "email_sent": email_sent
                    }
                else:
                    await safe_print("GAGAL - belum sampai payment page")
                    # Save debug screenshot
                    debug_path = os.path.join(SCRIPT_DIR, f"debug_{pembeli_num}_{attempt}.png")
                    try:
                        await page.screenshot(path=debug_path)
                        await safe_print(f"{prefix} Debug screenshot: {debug_path}")
                    except Exception:
                        pass
                    await page.close()
                    continue

            except Exception as e:
                await safe_print(f"{prefix} ERROR: {str(e)[:100]}")
                try:
                    await page.close()
                except Exception:
                    pass
                if attempt >= MAX_RETRY:
                    return {"nama": pembeli["nama"], "ok": False, "error": str(e)[:200]}
                continue

    finally:
        await browser.close()

    return {"nama": pembeli["nama"], "ok": False, "error": "Dihentikan paksa"}


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
    print("  BOT TIKET EXPLOREFOMO (Playwright Edition)")
    print("  Supports Faspay JS-rendered checkout pages")
    print("  Ctrl+C = stop + ringkasan")
    print("=" * 60)

    all_pembeli = load_data_pembeli()
    print(f"\n  Loaded: {len(all_pembeli)} akun dari data_pembeli.txt")

    # PILIHAN AKUN
    selected = select_accounts(all_pembeli)

    # INPUT CLI
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

    # Set pg_method ke semua pembeli terpilih
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

    print(f"  >> MULAI BELI TIKET (paralel via Playwright)...\n")

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

    # Fill missing results
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
