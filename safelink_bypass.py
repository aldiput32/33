#!/usr/bin/env python3
"""
Safelink Bypass Bot - Flexible URL extractor for any safelink/shortener.

Supports:
- Base64 encoded URLs (blogspot safelink, etc.)
- Common shorteners (bit.ly, tinyurl, s.id, etc.)
- Safelink redirectors (any domain with ?url=, ?link=, ?target= params)
- JavaScript-based redirectors (meta refresh, window.location, etc.)
- Queue/waiting room monitor (queue-it, tiket.com queue, etc.)
- Custom patterns (easily extensible)

Usage:
    python safelink_bypass.py <URL>
    python safelink_bypass.py --batch urls.txt
    python safelink_bypass.py --interactive
    python safelink_bypass.py --queue-wait <QUEUE_URL>
"""

import re
import sys
import time
import base64
import argparse
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from typing import Optional, List
from dataclasses import dataclass, field

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


@dataclass
class BypassResult:
    """Result of a bypass attempt."""
    original_url: str
    final_url: Optional[str] = None
    method: str = "unknown"
    success: bool = False
    error: Optional[str] = None
    chain: List[str] = field(default_factory=list)


class SafelinkBypass:
    """
    Flexible safelink/shortener bypass bot.
    Extracts the real destination URL from various safelink services.
    """

    # Common URL parameter names that contain the real destination
    URL_PARAMS = [
        "url", "link", "target", "dest", "destination", "redirect",
        "goto", "out", "u", "t", "r", "ref", "next", "continue",
        "return", "returnTo", "redirect_uri", "redirect_url",
    ]

    # Queue/waiting room domains — extract target URL from params
    QUEUE_DOMAINS = [
        "queue.tiket.com", "queue-it.net", "queue.website",
        "waitingroom.", "queue.", "antrian.",
    ]

    # Known safelink patterns (regex)
    SAFELINK_PATTERNS = [
        # Blogspot safelink (Base64 in ?url= param)
        r"blogspot\.com.*[?&]url=([A-Za-z0-9+/=]+)",
        # Generic base64 in hash fragment
        r"#([A-Za-z0-9+/=]{20,})",
        # Encoded URL in path segment
        r"/go/([A-Za-z0-9+/=_-]+)",
        r"/out/([A-Za-z0-9+/=_-]+)",
        r"/redirect/([A-Za-z0-9+/=_-]+)",
        r"/away/([A-Za-z0-9+/=_-]+)",
    ]

    # Shortener domains that need HTTP follow
    SHORTENER_DOMAINS = [
        "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly",
        "is.gd", "v.gd", "s.id", "shorturl.at", "rb.gy",
        "cutt.ly", "tiny.cc", "lnkd.in", "buff.ly",
    ]

    def __init__(self, timeout: int = 10, max_redirects: int = 10, 
                 user_agent: Optional[str] = None):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.session = None
        if HAS_REQUESTS:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": self.user_agent})

    def bypass(self, url: str) -> BypassResult:
        """
        Main entry point: attempt to bypass the given URL and extract the real link.
        Tries multiple methods in order of cost (cheapest first).
        """
        result = BypassResult(original_url=url)
        result.chain.append(url)

        # Method 0: Detect queue/waiting room systems (priority — always has target param)
        extracted = self._try_queue_system(url)
        if extracted:
            result.final_url = extracted
            result.method = "queue_bypass"
            result.success = True
            result.chain.append(extracted)
            return result

        # Method 1: Try extracting from URL parameters (no HTTP needed)
        extracted = self._try_url_params(url)
        if extracted:
            result.final_url = extracted
            result.method = "url_param_decode"
            result.success = True
            result.chain.append(extracted)
            return result

        # Method 2: Try known safelink regex patterns (no HTTP needed)
        extracted = self._try_safelink_patterns(url)
        if extracted:
            result.final_url = extracted
            result.method = "safelink_pattern"
            result.success = True
            result.chain.append(extracted)
            return result

        # Method 3: Try Base64 decode on path/fragment (no HTTP needed)
        extracted = self._try_base64_in_url(url)
        if extracted:
            result.final_url = extracted
            result.method = "base64_decode"
            result.success = True
            result.chain.append(extracted)
            return result

        # Method 4: Follow redirects via HTTP (requires requests)
        if HAS_REQUESTS and self.session:
            extracted = self._try_http_follow(url, result)
            if extracted:
                result.final_url = extracted
                result.method = "http_redirect"
                result.success = True
                return result

            # Method 5: Parse HTML for JS/meta redirects
            extracted = self._try_html_parse(url, result)
            if extracted:
                result.final_url = extracted
                result.method = "html_parse"
                result.success = True
                return result

        # Method 6: If URL is a direct link (no safelink detected), return as-is
        if self._is_direct_link(url):
            result.final_url = url
            result.method = "direct_link"
            result.success = True
            return result

        if not result.success:
            result.error = "Could not extract destination URL"

        return result

    def bypass_recursive(self, url: str, max_depth: int = 5) -> BypassResult:
        """
        Recursively bypass until we reach a non-safelink URL.
        Useful for chains of shorteners/safelinks.
        """
        visited = set()
        current_url = url
        final_result = BypassResult(original_url=url)
        final_result.chain.append(url)

        for _ in range(max_depth):
            if current_url in visited:
                break
            visited.add(current_url)

            result = self.bypass(current_url)
            if not result.success or result.final_url == current_url:
                break

            current_url = result.final_url
            final_result.chain.append(current_url)
            final_result.method = result.method

        final_result.final_url = current_url
        final_result.success = current_url != url
        if not final_result.success:
            final_result.error = "URL appears to be a direct link (no bypass needed)"
            final_result.final_url = url
            final_result.success = True

        return final_result

    # --- Private Methods ---

    def _try_queue_system(self, url: str) -> Optional[str]:
        """Detect queue/waiting room systems and extract the target URL."""
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = parsed.hostname or ""

            # Check if this is a known queue domain
            is_queue = any(
                q in hostname for q in self.QUEUE_DOMAINS
            )

            if is_queue:
                params = urllib.parse.parse_qs(parsed.query)
                # Queue systems commonly use 't', 'target', 'url' for destination
                queue_params = ["t", "target", "url", "redirect", "destination", 
                                "returnUrl", "return_url", "next"]
                for param_name in queue_params:
                    if param_name in params:
                        value = params[param_name][0]
                        decoded = urllib.parse.unquote(value)
                        if self._is_valid_url(decoded):
                            return decoded
                        if self._is_valid_url(value):
                            return value
        except Exception:
            pass
        return None

    def _is_direct_link(self, url: str) -> bool:
        """
        Check if URL is a direct/normal link (not a safelink/shortener).
        These should be returned as-is instead of failing.
        """
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = parsed.hostname or ""

            # Has a meaningful path (not just /)
            has_path = len(parsed.path.strip("/")) > 0

            # Not a known shortener
            is_shortener = any(
                hostname.endswith(s) for s in self.SHORTENER_DOMAINS
            )

            # Not a queue system
            is_queue = any(q in hostname for q in self.QUEUE_DOMAINS)

            return has_path and not is_shortener and not is_queue
        except Exception:
            return False

    def _try_url_params(self, url: str) -> Optional[str]:
        """Extract real URL from query parameters."""
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)

            for param_name in self.URL_PARAMS:
                if param_name in params:
                    value = params[param_name][0]
                    # Skip if value is too short to be a URL
                    if len(value) < 10:
                        continue
                    # Try Base64 decode first
                    decoded = self._decode_base64(value)
                    if decoded and self._is_valid_url(decoded):
                        return decoded
                    # Try URL decode
                    decoded = urllib.parse.unquote(value)
                    if self._is_valid_url(decoded):
                        return decoded
                    # Maybe it's already a valid URL
                    if self._is_valid_url(value):
                        return value
        except Exception:
            pass
        return None

    def _try_safelink_patterns(self, url: str) -> Optional[str]:
        """Try known safelink regex patterns."""
        for pattern in self.SAFELINK_PATTERNS:
            match = re.search(pattern, url)
            if match:
                encoded = match.group(1)
                decoded = self._decode_base64(encoded)
                if decoded and self._is_valid_url(decoded):
                    return decoded
                # Try URL-safe base64
                decoded = self._decode_base64_urlsafe(encoded)
                if decoded and self._is_valid_url(decoded):
                    return decoded
        return None

    def _try_base64_in_url(self, url: str) -> Optional[str]:
        """Try to find and decode Base64 strings in URL parts."""
        parsed = urllib.parse.urlparse(url)

        # Check fragment
        if parsed.fragment:
            decoded = self._decode_base64(parsed.fragment)
            if decoded and self._is_valid_url(decoded):
                return decoded

        # Check last path segment
        path_parts = parsed.path.strip("/").split("/")
        if path_parts:
            last_part = path_parts[-1]
            if len(last_part) > 10:
                decoded = self._decode_base64(last_part)
                if decoded and self._is_valid_url(decoded):
                    return decoded
                decoded = self._decode_base64_urlsafe(last_part)
                if decoded and self._is_valid_url(decoded):
                    return decoded
        return None

    def _try_http_follow(self, url: str, result: BypassResult) -> Optional[str]:
        """Follow HTTP redirects to find the final URL."""
        try:
            resp = self.session.head(
                url,
                allow_redirects=True,
                timeout=self.timeout,
            )
            final = resp.url
            if final != url:
                result.chain.append(final)
                return final

            # Try GET if HEAD didn't redirect
            resp = self.session.get(
                url,
                allow_redirects=True,
                timeout=self.timeout,
            )
            if resp.url != url:
                result.chain.append(resp.url)
                return resp.url
        except Exception:
            pass
        return None

    def _try_html_parse(self, url: str, result: BypassResult) -> Optional[str]:
        """Parse HTML page for JavaScript/meta redirects."""
        try:
            resp = self.session.get(url, timeout=self.timeout)
            html = resp.text

            # Meta refresh tag
            meta_match = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][\d;]*\s*url=([^"\'>\s]+)',
                html, re.IGNORECASE
            )
            if meta_match:
                target = meta_match.group(1)
                if self._is_valid_url(target):
                    result.chain.append(target)
                    return target

            # JavaScript window.location patterns
            js_patterns = [
                r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
                r'window\.location\.replace\s*\(\s*["\']([^"\']+)["\']',
                r'window\.location\s*=\s*["\']([^"\']+)["\']',
                r'document\.location\.href\s*=\s*["\']([^"\']+)["\']',
                r'top\.location\.href\s*=\s*["\']([^"\']+)["\']',
                r'window\.open\s*\(\s*["\']([^"\']+)["\']',
            ]
            for pattern in js_patterns:
                match = re.search(pattern, html)
                if match:
                    target = match.group(1)
                    if self._is_valid_url(target):
                        result.chain.append(target)
                        return target

            # Look for Base64 encoded strings in JavaScript
            b64_matches = re.findall(
                r'atob\s*\(\s*["\']([A-Za-z0-9+/=]+)["\']',
                html
            )
            for b64 in b64_matches:
                decoded = self._decode_base64(b64)
                if decoded and self._is_valid_url(decoded):
                    result.chain.append(decoded)
                    return decoded

            # BeautifulSoup for more complex parsing
            if HAS_REQUESTS:
                soup = BeautifulSoup(html, "html.parser")

                # Check for hidden links or buttons with data attributes
                for elem in soup.find_all(attrs={"data-url": True}):
                    target = elem["data-url"]
                    decoded = self._decode_base64(target)
                    if decoded and self._is_valid_url(decoded):
                        result.chain.append(decoded)
                        return decoded
                    if self._is_valid_url(target):
                        result.chain.append(target)
                        return target

                # Check for countdown/timer pages with link in script
                scripts = soup.find_all("script")
                for script in scripts:
                    if script.string:
                        # Look for var link = "..." or var url = "..."
                        var_match = re.search(
                            r'(?:var|let|const)\s+(?:link|url|href|redirect|destination)\s*=\s*["\']([^"\']+)["\']',
                            script.string
                        )
                        if var_match:
                            target = var_match.group(1)
                            if self._is_valid_url(target):
                                result.chain.append(target)
                                return target

        except Exception:
            pass
        return None

    # --- Utility Methods ---

    def _decode_base64(self, encoded: str) -> Optional[str]:
        """Decode standard Base64 string."""
        try:
            # Add padding if needed
            padding = 4 - len(encoded) % 4
            if padding != 4:
                encoded += "=" * padding
            decoded = base64.b64decode(encoded).decode("utf-8")
            return decoded
        except Exception:
            return None

    def _decode_base64_urlsafe(self, encoded: str) -> Optional[str]:
        """Decode URL-safe Base64 string."""
        try:
            padding = 4 - len(encoded) % 4
            if padding != 4:
                encoded += "=" * padding
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
            return decoded
        except Exception:
            return None

    def _is_valid_url(self, url: str) -> bool:
        """Check if a string looks like a valid URL."""
        if not url:
            return False
        try:
            parsed = urllib.parse.urlparse(url)
            return parsed.scheme in ("http", "https") and bool(parsed.netloc)
        except Exception:
            return False


class QueueWaiter:
    """
    Queue/Waiting Room monitor.
    Polls the queue page and waits until the queue passes,
    then automatically opens the target URL.
    """

    def __init__(self, queue_url: str, interval: int = 5, timeout_minutes: int = 60,
                 open_browser: bool = True, user_agent: Optional[str] = None):
        self.queue_url = queue_url
        self.interval = interval
        self.timeout_minutes = timeout_minutes
        self.open_browser = open_browser
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        # Extract target URL from queue params
        self.bypass = SafelinkBypass(user_agent=self.user_agent)
        self.target_url = self.bypass._try_queue_system(queue_url)
        if not self.target_url:
            # Fallback: try generic param extraction
            self.target_url = self.bypass._try_url_params(queue_url)

        # Extract event info from queue URL
        parsed = urllib.parse.urlparse(queue_url)
        params = urllib.parse.parse_qs(parsed.query)
        self.event_name = params.get("l", params.get("e", ["Unknown Event"]))[0]
        self.customer = params.get("c", ["unknown"])[0]

        # Session for maintaining cookies
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })

        # Stats
        self.start_time = None
        self.poll_count = 0
        self.last_status = None

    def wait(self) -> bool:
        """
        Main loop: poll the queue until it passes or timeout.
        Returns True if queue passed, False if timed out.
        """
        if not HAS_REQUESTS:
            print("[-] Error: 'requests' library required. Install with: pip install requests")
            return False

        self.start_time = datetime.now()
        deadline = self.start_time + timedelta(minutes=self.timeout_minutes)

        print("=" * 64)
        print("  QUEUE WAITING ROOM MONITOR")
        print("=" * 64)
        print(f"  Event    : {self.event_name}")
        print(f"  Customer : {self.customer}")
        print(f"  Target   : {self.target_url or 'detecting...'}")
        print(f"  Interval : {self.interval}s")
        print(f"  Timeout  : {self.timeout_minutes} min")
        print(f"  Started  : {self.start_time.strftime('%H:%M:%S')}")
        print("=" * 64)
        print()
        print("[*] Monitoring queue status... (Ctrl+C to stop)")
        print()

        # First: hit the queue URL to establish session/cookies
        try:
            resp = self.session.get(self.queue_url, allow_redirects=False, timeout=15)
            self._print_status(resp, initial=True)
        except Exception as e:
            print(f"[!] Initial request failed: {e}")
            print("[*] Will keep retrying...")

        # Main polling loop
        while datetime.now() < deadline:
            try:
                time.sleep(self.interval)
                self.poll_count += 1

                # Strategy 1: Check queue page — see if it redirects to target
                passed = self._check_queue_page()
                if passed:
                    return self._on_queue_passed()

                # Strategy 2: Directly check if target URL is accessible 
                # (not redirecting back to queue)
                if self.target_url:
                    target_ok = self._check_target_accessible()
                    if target_ok:
                        return self._on_queue_passed()

            except KeyboardInterrupt:
                elapsed = datetime.now() - self.start_time
                print(f"\n[!] Stopped by user after {self._format_duration(elapsed)}")
                print(f"[*] Total polls: {self.poll_count}")
                return False
            except Exception as e:
                print(f"  [{self._timestamp()}] Error: {e} (retrying...)")

        # Timeout
        elapsed = datetime.now() - self.start_time
        print(f"\n[-] Timeout after {self._format_duration(elapsed)}")
        print(f"[-] Queue did not pass within {self.timeout_minutes} minutes")
        print(f"[*] Total polls: {self.poll_count}")
        return False

    def _check_queue_page(self) -> bool:
        """
        Poll the queue URL and check if:
        1. It redirects to target (302/301 to non-queue URL)
        2. The HTML no longer shows a waiting room
        3. It returns a token/cookie that allows target access
        """
        try:
            resp = self.session.get(
                self.queue_url,
                allow_redirects=False,
                timeout=15
            )

            # Check for redirect to target
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if location:
                    # Resolve relative URLs
                    if not location.startswith("http"):
                        location = urllib.parse.urljoin(self.queue_url, location)

                    # Skip error pages and same-domain queue redirects
                    if "/error" in location or "er=" in location:
                        self._print_poll(f"Queue error page (token expired?)")
                        return False

                    if not self._is_queue_url(location):
                        # Verify it's actually the target or a non-queue page
                        if self.target_url and self.target_url in location:
                            print(f"\n  [{self._timestamp()}] REDIRECT to target -> {location}")
                            return True
                        elif not self._is_queue_url(location):
                            print(f"\n  [{self._timestamp()}] REDIRECT detected -> {location}")
                            if not self.target_url:
                                self.target_url = location
                            return True

            # Check if response is the actual target page (200 with non-queue content)
            if resp.status_code == 200:
                content = resp.text.lower()

                # Signs that queue has passed
                queue_passed_indicators = [
                    "queue" not in content and "waiting" not in content and "antri" not in content,
                    "your turn" in content,
                    "you have been redirected" in content,
                    "redirecting you" in content,
                ]

                # Signs still in queue
                still_in_queue_indicators = [
                    "waiting room" in content,
                    "in queue" in content,
                    "your estimated" in content,
                    "people ahead" in content,
                    "queue-it" in content,
                    "antrian" in content,
                    "mohon tunggu" in content,
                    "please wait" in content,
                    "you are now in line" in content,
                ]

                # Extract queue position if available
                position = self._extract_queue_position(resp.text)

                if any(still_in_queue_indicators):
                    status = f"Still in queue"
                    if position:
                        status += f" | Position: {position}"
                    self._print_poll(status)
                    return False

                if any(queue_passed_indicators):
                    print(f"\n  [{self._timestamp()}] Queue page content changed!")
                    return True

                # Default: still waiting
                self._print_poll("Waiting...")
                return False

            # Non-200 status
            self._print_poll(f"HTTP {resp.status_code}")
            return False

        except requests.exceptions.Timeout:
            self._print_poll("Timeout (server busy)")
            return False
        except Exception as e:
            self._print_poll(f"Error: {str(e)[:50]}")
            return False

    def _check_target_accessible(self) -> bool:
        """
        Check if the target URL is directly accessible without 
        being redirected back to a queue page.
        """
        try:
            resp = self.session.get(
                self.target_url,
                allow_redirects=False,
                timeout=10
            )

            # If target returns 200, queue has passed
            if resp.status_code == 200:
                content = resp.text.lower()
                # Make sure it's not a queue page disguised as 200
                if "queue" not in content and "waiting room" not in content:
                    return True

            # If redirect back to queue, still waiting
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if self._is_queue_url(location):
                    return False
                # Redirect to non-queue URL = passed
                return True

            return False
        except Exception:
            return False

    def _is_queue_url(self, url: str) -> bool:
        """Check if URL is a queue/waiting room URL."""
        try:
            parsed = urllib.parse.urlparse(url)
            hostname = parsed.hostname or ""
            return any(q in hostname for q in SafelinkBypass.QUEUE_DOMAINS)
        except Exception:
            return False

    def _extract_queue_position(self, html: str) -> Optional[str]:
        """Try to extract queue position from HTML."""
        patterns = [
            r'(?:position|posisi|nomor)[\s:]*(\d[\d,\.]*)',
            r'(\d[\d,\.]*)\s*(?:people|orang|users?)\s*(?:ahead|di depan)',
            r'(?:ahead of you|di depan anda)[\s:]*(\d[\d,\.]*)',
            r'"queuePosition"[\s:]*(\d+)',
            r'"usersInLineAheadOfYou"[\s:]*(\d+)',
            r'data-queue-position="(\d+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _on_queue_passed(self) -> bool:
        """Called when queue has passed."""
        elapsed = datetime.now() - self.start_time

        print()
        print("=" * 64)
        print("  ✓ QUEUE PASSED!")
        print("=" * 64)
        print(f"  Target URL : {self.target_url}")
        print(f"  Wait time  : {self._format_duration(elapsed)}")
        print(f"  Polls      : {self.poll_count}")
        print(f"  Passed at  : {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 64)

        # Open in browser
        if self.open_browser and self.target_url:
            print(f"\n[+] Opening target URL in browser...")
            try:
                webbrowser.open(self.target_url)
            except Exception:
                print(f"[!] Could not open browser. URL: {self.target_url}")

        # Export cookies for manual use
        self._export_session_info()

        return True

    def _export_session_info(self):
        """Export session cookies so user can use them in browser."""
        cookies = self.session.cookies.get_dict()
        if cookies:
            print(f"\n[+] Session cookies (use in browser if needed):")
            for name, value in cookies.items():
                print(f"    {name}={value[:50]}{'...' if len(value) > 50 else ''}")

    def _print_status(self, resp, initial=False):
        """Print initial connection status."""
        prefix = "Initial" if initial else "Status"
        print(f"  [{self._timestamp()}] {prefix}: HTTP {resp.status_code}")
        if resp.cookies:
            print(f"  [{self._timestamp()}] Cookies received: {len(resp.cookies)}")

    def _print_poll(self, status: str):
        """Print poll status on same line (overwrite)."""
        elapsed = datetime.now() - self.start_time
        elapsed_str = self._format_duration(elapsed)
        line = f"  [{self._timestamp()}] Poll #{self.poll_count:>4} | {elapsed_str} | {status}"
        # Use carriage return to overwrite previous line
        sys.stdout.write(f"\r{line:<80}")
        sys.stdout.flush()

    def _timestamp(self) -> str:
        """Get current timestamp string."""
        return datetime.now().strftime("%H:%M:%S")

    def _format_duration(self, td: timedelta) -> str:
        """Format timedelta as human readable."""
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


def main():
    parser = argparse.ArgumentParser(
        description="Safelink Bypass Bot - Extract real URLs from any safelink/shortener",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python safelink_bypass.py "https://example.com/safelink?url=aHR0cHM6Ly9nb29nbGUuY29t"
  python safelink_bypass.py --batch urls.txt
  python safelink_bypass.py --interactive
  python safelink_bypass.py --recursive "https://bit.ly/xyz"
  python safelink_bypass.py --queue-wait "https://queue.tiket.com/?c=tiket&e=event&t=https://target.com"
        """
    )
    parser.add_argument("url", nargs="?", help="URL to bypass")
    parser.add_argument("--batch", "-b", help="File with URLs (one per line)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--recursive", "-r", action="store_true", 
                        help="Recursively follow redirects/safelinks")
    parser.add_argument("--timeout", "-t", type=int, default=10, help="HTTP timeout (seconds)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show redirect chain")

    # Queue waiting room options
    parser.add_argument("--queue-wait", "-qw", metavar="URL",
                        help="Monitor a queue/waiting room URL until it passes")
    parser.add_argument("--queue-interval", "-qi", type=int, default=5,
                        help="Queue poll interval in seconds (default: 5)")
    parser.add_argument("--queue-timeout", "-qt", type=int, default=60,
                        help="Queue timeout in minutes (default: 60)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser when queue passes")

    args = parser.parse_args()

    # Queue wait mode
    if args.queue_wait:
        if not HAS_REQUESTS:
            print("[-] Error: 'requests' library required.")
            print("    Install with: pip install requests beautifulsoup4")
            sys.exit(1)

        waiter = QueueWaiter(
            queue_url=args.queue_wait,
            interval=args.queue_interval,
            timeout_minutes=args.queue_timeout,
            open_browser=not args.no_browser,
        )
        success = waiter.wait()
        sys.exit(0 if success else 1)

    bot = SafelinkBypass(timeout=args.timeout)

    def process_url(url: str):
        url = url.strip()
        if not url or url.startswith("#"):
            return

        if args.recursive:
            result = bot.bypass_recursive(url)
        else:
            result = bot.bypass(url)

        if result.success:
            print(f"\n[+] Original : {result.original_url}")
            print(f"[+] Result   : {result.final_url}")
            print(f"[+] Method   : {result.method}")
            if args.verbose and len(result.chain) > 2:
                print(f"[+] Chain    : {' -> '.join(result.chain)}")
        else:
            print(f"\n[-] Failed   : {result.original_url}")
            print(f"[-] Error    : {result.error}")

    # Interactive mode
    if args.interactive:
        print("=" * 60)
        print("  Safelink Bypass Bot - Interactive Mode")
        print("  Type a URL and press Enter. Type 'quit' to exit.")
        print("  Prefix with 'wait:' to monitor a queue URL.")
        print("=" * 60)
        while True:
            try:
                url = input("\n> Enter URL: ").strip()
                if url.lower() in ("quit", "exit", "q"):
                    print("Bye!")
                    break
                if url.lower().startswith("wait:"):
                    queue_url = url[5:].strip()
                    if queue_url and HAS_REQUESTS:
                        waiter = QueueWaiter(
                            queue_url=queue_url,
                            interval=args.queue_interval if hasattr(args, 'queue_interval') else 5,
                            timeout_minutes=args.queue_timeout if hasattr(args, 'queue_timeout') else 60,
                            open_browser=not args.no_browser if hasattr(args, 'no_browser') else True,
                        )
                        waiter.wait()
                    elif not HAS_REQUESTS:
                        print("[-] 'requests' library required for queue monitoring")
                    else:
                        print("[-] Usage: wait:<queue_url>")
                elif url:
                    process_url(url)
            except (KeyboardInterrupt, EOFError):
                print("\nBye!")
                break
        return

    # Batch mode
    if args.batch:
        try:
            with open(args.batch, "r") as f:
                urls = f.readlines()
            print(f"[*] Processing {len(urls)} URLs from {args.batch}...")
            for url in urls:
                process_url(url)
        except FileNotFoundError:
            print(f"[-] File not found: {args.batch}")
            sys.exit(1)
        return

    # Single URL mode
    if args.url:
        process_url(args.url)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
