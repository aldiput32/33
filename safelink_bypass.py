#!/usr/bin/env python3
"""
Safelink Bypass Bot - Flexible URL extractor for any safelink/shortener.

Supports:
- Base64 encoded URLs (blogspot safelink, etc.)
- Common shorteners (bit.ly, tinyurl, s.id, etc.)
- Safelink redirectors (any domain with ?url=, ?link=, ?target= params)
- JavaScript-based redirectors (meta refresh, window.location, etc.)
- Custom patterns (easily extensible)

Usage:
    python safelink_bypass.py <URL>
    python safelink_bypass.py --batch urls.txt
    python safelink_bypass.py --interactive
"""

import re
import sys
import base64
import argparse
import urllib.parse
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
        "goto", "out", "u", "q", "r", "ref", "next", "continue",
        "return", "returnTo", "redirect_uri", "redirect_url",
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

    def _try_url_params(self, url: str) -> Optional[str]:
        """Extract real URL from query parameters."""
        try:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)

            for param_name in self.URL_PARAMS:
                if param_name in params:
                    value = params[param_name][0]
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
        """
    )
    parser.add_argument("url", nargs="?", help="URL to bypass")
    parser.add_argument("--batch", "-b", help="File with URLs (one per line)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--recursive", "-r", action="store_true", 
                        help="Recursively follow redirects/safelinks")
    parser.add_argument("--timeout", "-t", type=int, default=10, help="HTTP timeout (seconds)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show redirect chain")

    args = parser.parse_args()
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
        print("=" * 60)
        while True:
            try:
                url = input("\n> Enter URL: ").strip()
                if url.lower() in ("quit", "exit", "q"):
                    print("Bye!")
                    break
                if url:
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
