from contextlib import closing
from http.client import RemoteDisconnected
from math import ceil
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.request import urlopen as _raw_urlopen, Request
import os
import sqlite3
import hashlib
import pickle
import io
import threading
from email.message import Message
import json
import time
import concurrent.futures

# Do not import calibre.browser at module import time to avoid creating Qt-backed
# network/browser objects on worker threads. Use urllib for worker-thread fetches.
from calibre.gui2 import open_url
from calibre.gui2.store import StorePlugin
import threading as _threading
try:
    # QObject-based executor to run callables on the GUI thread via queued signal
    from PyQt5.QtCore import QObject, pyqtSignal, Qt

    class _GuiExecutor(QObject):
        run = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            # connect with queued connection so calls from other threads are executed on this object's thread
            self.run.connect(self._on_run, Qt.QueuedConnection)

        def _on_run(self, func):
            try:
                func()
            except Exception:
                pass

    # create executor instance on import (plugin import happens on GUI thread in Calibre)
    try:
        _gui_executor = _GuiExecutor()
    except Exception:
        _gui_executor = None
except Exception:
    _gui_executor = None

# If debugging enabled, ensure the Qt message handler is installed from the GUI thread
if os.environ.get('ANN_DEBUG_QT') and _gui_executor is not None:
    try:
        def _install_handler():
            try:
                from PyQt5.QtCore import qInstallMessageHandler
                import traceback, sys, threading as _dbg_threading

                def _qt_msg_handler(msg_type, context, message):
                    try:
                        s = str(message)
                        if 'QBasicTimer::start' in s:
                            print('\n=== Qt message (captured) ===', file=sys.stderr)
                            print(s, file=sys.stderr)
                            print('Thread:', _dbg_threading.current_thread().name, file=sys.stderr)
                            traceback.print_stack(file=sys.stderr)
                            print('=== end Qt message ===\n', file=sys.stderr)
                    except Exception:
                        pass

                qInstallMessageHandler(_qt_msg_handler)
            except Exception:
                pass

        # dispatch installer on GUI thread
        try:
            _gui_executor.run.emit(_install_handler)
        except Exception:
            # fallback: try direct install
            _install_handler()
    except Exception:
        pass

# Additionally, for robustness, wrap sys.stderr to capture any plain stderr messages
# (some Qt warnings may bypass qInstallMessageHandler depending on build). This wrapper
# will print a Python stack trace whenever the QBasicTimer warning is written to stderr.
if os.environ.get('ANN_DEBUG_QT'):
    try:
        import sys, traceback, threading as _dbg_threading
        _orig_stderr = sys.stderr

        class _StderrWrapper:
            def write(self, s):
                try:
                    if isinstance(s, str) and 'QBasicTimer::start' in s:
                        try:
                            _orig_stderr.write('\n=== QBasicTimer warning (captured by annas_archive) ===\n')
                            _orig_stderr.write(s + '\n')
                            _orig_stderr.write(f'Thread: {_dbg_threading.current_thread().name}\n')
                            traceback.print_stack(file=_orig_stderr)
                            _orig_stderr.write('=== end QBasicTimer warning ===\n')
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    return _orig_stderr.write(s)
                except Exception:
                    return None

            def flush(self):
                try:
                    return _orig_stderr.flush()
                except Exception:
                    return None

            def fileno(self):
                try:
                    return _orig_stderr.fileno()
                except Exception:
                    return 2

        sys.stderr = _StderrWrapper()
    except Exception:
        pass

# Avoid instantiating Calibre's SearchResult on non-main threads (it may create Qt objects
# such as timers). Use a lightweight fallback object when running off the main thread.
try:
    from calibre.gui2.store.search_result import SearchResult as _CalibreSearchResult
except Exception:
    _CalibreSearchResult = None


class _FallbackSearchResult:
    DRM_UNLOCKED = 'DRM_UNLOCKED'

    def __init__(self):
        self.formats = ''
        self.downloads = {}
        self.detail_item = None
        self.cover_url = ''
        self.title = ''
        self.author = ''
        self.price = ''
        self.drm = None
        # Provide minimal attributes expected by Calibre UI code
        # cover_data is used by calibre.gui2.store.search.models to draw covers
        self.cover_data = None
        # Some UI code may inspect cover size or image bytes; keep a fallback property
        self.cover_bytes = None


def _make_search_result():
    # Return Calibre SearchResult only when on main thread; otherwise use fallback.
    if _CalibreSearchResult is not None and _threading.current_thread() is _threading.main_thread():
        return _CalibreSearchResult()
    return _FallbackSearchResult()

# Expose SearchResult name for type aliases and external imports. Prefer Calibre's when available.
SearchResult = _CalibreSearchResult or _FallbackSearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog
from calibre_plugins.store_annas_archive.constants import DEFAULT_MIRRORS, RESULTS_PER_PAGE, SearchOption
from lxml import html
import re

try:
    from qt.core import QUrl
except (ImportError, ModuleNotFoundError):
    from PyQt5.Qt import QUrl

# expose a module-level urlopen alias so tests can patch annas_archive.urlopen
urlopen = _raw_urlopen
SearchResults = Generator[SearchResult, None, None]

# If requested, install a Qt message handler to capture QBasicTimer warnings and
# print a Python stacktrace for debugging. Enable by setting ANN_DEBUG_QT=1.
if os.environ.get('ANN_DEBUG_QT'):
    try:
        from PyQt5.QtCore import qInstallMessageHandler
        import traceback, sys
        import threading as _dbg_threading

        def _qt_msg_handler(*args):
            try:
                # message is last arg
                msg = args[-1]
                s = str(msg)
                if 'QBasicTimer::start' in s:
                    print('\n=== Qt message (captured) ===', file=sys.stderr)
                    print(s, file=sys.stderr)
                    print('Thread:', _dbg_threading.current_thread().name, file=sys.stderr)
                    traceback.print_stack(file=sys.stderr)
                    print('=== end Qt message ===\n', file=sys.stderr)
            except Exception:
                pass

        qInstallMessageHandler(_qt_msg_handler)
    except Exception:
        # best-effort only; do not fail plugin import
        pass


class AnnasArchiveStore(StorePlugin):

    def __init__(self, gui, name, config=None, base_plugin=None):
        super().__init__(gui, name, config, base_plugin)
        self.working_mirror = None
        # setup simple on-disk HTTP cache for provider / detail page responses
        # cache is optional and controlled by env var ANN_CACHE_DIR; if unset no caching
        cache_dir = os.environ.get('ANN_CACHE_DIR')
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self._cache_dir = cache_dir
        # TTL for cache entries in seconds. If 0 or unset, entries never expire.
        try:
            self._cache_ttl = int(os.environ.get('ANN_CACHE_TTL', '0'))
        except Exception:
            self._cache_ttl = 0
        # small in-memory lock per cache file to avoid races
        self._cache_locks = {}
        # store cookies observed during provider resolution keyed by domain
        self._provider_cookies = {}

    def create_browser(self):
        """Return a mechanize.Browser configured with cookies captured during provider resolution.

        Calibre's download jobs call the plugin's create_browser to obtain a mechanize browser
        used to perform the actual file download. We populate the browser's cookiejar with
        cookies collected during provider page fetches so downloads that require those cookies
        (DDOS-protection) succeed.
        """
        try:
            import mechanize
            import http.cookiejar as cookiejar
        except Exception:
            # mechanize unavailable; fall back to base implementation
            return super().create_browser()

        br = mechanize.Browser()
        cj = cookiejar.CookieJar()

        def _add_to_jar(jar, domain, cookie_str):
            host = domain.split(':', 1)[0]
            for part in cookie_str.split(';'):
                part = part.strip()
                if not part:
                    continue
                if '=' in part:
                    name, val = part.split('=', 1)
                else:
                    name, val = part, ''
                # build a Cookie object
                try:
                    c = cookiejar.Cookie(
                        version=0,
                        name=name,
                        value=val,
                        port=None,
                        port_specified=False,
                        domain=host,
                        domain_specified=True,
                        domain_initial_dot=host.startswith('.'),
                        path='/',
                        path_specified=True,
                        secure=False,
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                    jar.set_cookie(c)
                except Exception:
                    # best-effort: ignore cookie creation failures
                    continue

        for domain, cookie_str in (self._provider_cookies or {}).items():
            try:
                _add_to_jar(cj, domain, cookie_str)
            except Exception:
                continue

        br.set_cookiejar(cj)
        # set common headers similar to a modern browser
        br.addheaders = [
            ('User-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'),
            ('Accept-Encoding', 'identity')
        ]
        # be tolerant: do not obey robots.txt and allow redirects
        try:
            br.set_handle_robots(False)
        except Exception:
            pass
        try:
            br.set_handle_redirect(True)
        except Exception:
            pass

        return br

    def _search(self, url: str, max_results: int, timeout: int) -> SearchResults:
        # debug: optionally print thread info when searching
        if os.environ.get('ANN_DEBUG_QT'):
            import threading, sys
            print(f"[annas_archive._search] thread={threading.current_thread().name}", file=sys.stderr)
        # Avoid using calibre.browser() on worker threads (it may create Qt objects).
        doc = None
        counter = max_results

        for page in range(1, ceil(max_results / RESULTS_PER_PAGE) + 1):
            # copy mirrors from config to avoid mutating stored config
            mirrors = list(self.config.get('mirrors', DEFAULT_MIRRORS))
            # prefer last working mirror by trying it first
            if self.working_mirror is not None:
                if self.working_mirror in mirrors:
                    mirrors.remove(self.working_mirror)
                mirrors.insert(0, self.working_mirror)

            # reorder mirrors by cached health and probe unknown/stale mirrors in parallel
            try:
                mirrors = self._order_mirrors(mirrors, timeout)
            except Exception:
                # on any probe error, fall back to original order
                pass

            for mirror in mirrors:
                try:
                    req_url = url.format(base=mirror, page=page)
                    with closing(urlopen(Request(req_url), timeout=timeout)) as resp:
                        # skip server 5xx errors
                        if 500 <= getattr(resp, 'code', 0) <= 599:
                            continue
                        self.working_mirror = mirror
                        doc = html.fromstring(resp.read())
                        break
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError):
                    # try next mirror on network/error
                    continue
            if doc is None:
                self.working_mirror = None
                raise Exception('No working mirrors of Anna\'s Archive found.')

            books = doc.xpath('//table/tr')
            for book in books:
                if counter <= 0:
                    break

                columns = book.findall("td")
                # guard against unexpected table layout
                if len(columns) < 10:
                    continue
                # create a SearchResult appropriate for thread context
                s = _make_search_result()

                cover = columns[0].xpath('./a[@tabindex="-1"]')
                if cover:
                    cover = cover[0]
                else:
                    continue
                s.detail_item = cover.get('href', '').split('/')[-1]
                if not s.detail_item:
                    continue

                s.cover_url = ''.join(cover.xpath('(./span/img/@src)[1]'))
                s.title = ''.join(columns[1].xpath('./a/span/text()'))
                s.author = ''.join(columns[2].xpath('./a/span/text()'))
                s.formats = ''.join(columns[9].xpath('./a/span/text()')).upper()

                s.price = '$0.00'
                s.drm = SearchResult.DRM_UNLOCKED

                counter -= 1
                # if we returned a fallback result but we can create real SearchResult on GUI thread,
                # convert it there and yield the real object to the caller. If GUI executor not available,
                # yield the fallback.
                if isinstance(s, _FallbackSearchResult) and _CalibreSearchResult is not None and _gui_executor is not None:
                    # prepare a container to receive the real result
                    container = {}

                    def make_real():
                        try:
                            real = _CalibreSearchResult()
                            # copy simple fields
                            real.formats = s.formats
                            real.downloads = s.downloads
                            real.detail_item = s.detail_item
                            real.cover_url = s.cover_url
                            real.title = s.title
                            real.author = s.author
                            real.price = s.price
                            real.drm = s.drm
                            # assign to container
                            container['real'] = real
                        except Exception:
                            container['real'] = s

                    # dispatch to GUI thread and wait briefly for execution
                    event = _threading.Event()

                    def wrapper():
                        make_real()
                        event.set()

                    try:
                        _gui_executor.run.emit(wrapper)
                        # wait up to a short timeout for GUI thread to process
                        event.wait(1.0)
                    except Exception:
                        pass

                    yield container.get('real', s)
                else:
                    yield s

    def search(self, query, max_results=10, timeout=60) -> SearchResults:
        url = f'{{base}}/search?page={{page}}&q={quote_plus(query)}&display=table'
        search_opts = self.config.get('search', {})
        for option in SearchOption.options:
            value = search_opts.get(option.config_option, ())
            if isinstance(value, str):
                value = (value,)
            for item in value:
                url += f'&{option.url_param}={item}'
        yield from self._search(url, max_results, timeout)

    def open(self, parent=None, detail_item=None, external=False):
        if detail_item:
            url = self._get_url(detail_item)
        else:
            if self.working_mirror is not None:
                url = self.working_mirror
            else:
                url = self.config.get('mirrors', DEFAULT_MIRRORS)[0]
        if external or self.config.get('open_external', False):
            open_url(QUrl(url))
        else:
            d = WebStoreDialog(self.gui, self.working_mirror, parent, url)
            d.setWindowTitle(self.name)
            d.set_tags(self.config.get('tags', ''))
            d.exec()

    def get_details(self, search_result: SearchResult, timeout=60):
        if os.environ.get('ANN_DEBUG_QT'):
            import threading, sys
            print(f"[annas_archive.get_details] thread={threading.current_thread().name}", file=sys.stderr)
        if not search_result.formats:
            return

        # Hide download entries. We intentionally do not populate
        # search_result.downloads to avoid exposing provider links or
        # triggering provider fetches. This keeps the Calibre UI from
        # showing download buttons after a search.
        try:
            # replace or clear existing downloads mapping
            search_result.downloads = {}
        except Exception:
            pass
        return

        _format = '.' + search_result.formats.lower()

        link_opts = self.config.get('link', {})
        url_extension = link_opts.get('url_extension', True)
        content_type = link_opts.get('content_type', False)

        # fetch detail page (use cache if enabled)
        detail_url = self._get_url(search_result.detail_item)
        body = None
        if self._cache_dir:
            body = self._cache_get(detail_url)
        if body is None:
            with closing(urlopen(Request(detail_url), timeout=timeout)) as f:
                body = f.read()
            if self._cache_dir:
                self._cache_set(detail_url, body)
        doc = html.fromstring(body)

        # select all anchors under the downloads panel. Some mirrors/pages don't include
        # the 'js-download-link' class server-side, so be permissive and filter later.
        links = doc.xpath('//div[@id="md5-panel-downloads"]//a[@href]')

        base_page_url = self._get_url(search_result.detail_item)

        # Do not fetch provider pages. Some providers enforce anti-bot challenges and
        # direct resolution causes 403s. Instead, expose the provider anchor hrefs so
        # the user (or Calibre) can follow them in the browser. This avoids network
        # calls to provider pages here.
        resolved = {}
        for a in links:
            href = a.get('href')
            if not href:
                continue
            link_text = ''.join(a.itertext()).strip()
            abs_href = urljoin(base_page_url, href)
            resolved[link_text] = abs_href

        for link_text, url in resolved.items():
            if not url:
                continue

            # Do not perform provider HEAD/content-type checks. Provider pages often
            # implement anti-bot measures (Cloudflare/ddos-guard) and attempting to
            # fetch them here causes 403s. We only expose the anchor hrefs and
            # optionally filter by URL extension below.
            if url_extension and not content_type:
                # Speeds it up by checking the extension of the url. Strip query params first.
                path = url.split('?', 1)[0]
                if not path.lower().endswith(_format):
                    continue

            # build a friendly label for the download entry. Prefer the book title and a
            # short provider hint (domain or provider name). Avoid using generic anchor
            # text like 'Slow partner server #1'.
            def _provider_from_url(u: str) -> str:
                try:
                    host = urlparse(u).netloc.split(':', 1)[0]
                    parts = host.split('.')
                    if len(parts) >= 2:
                        base = parts[-2]
                    else:
                        base = parts[0]
                    mapping = {
                        'libgen': 'LibGen',
                        'annas-archive': "Anna's Archive",
                        'annasarchive': "Anna's Archive",
                        'z-lib': 'Z-Library',
                        'zlib': 'Z-Library',
                        'sci-hub': 'Sci-Hub',
                        'scihub': 'Sci-Hub'
                    }
                    return mapping.get(base.lower(), base.capitalize())
                except Exception:
                    return urlparse(u).netloc or 'provider'

            text = (link_text or '').strip()
            # treat generic anchor text as unhelpful
            if not text or re.search(r'\b(partner|server|slow|fast|partner server|download|direct)\b', text, re.I):
                provider_label = _provider_from_url(url)
            else:
                provider_label = text

            # prefer title as main label; include provider short hint
            title = getattr(search_result, 'title', '') or ''
            if title:
                base_label = f"{title} ({provider_label})"
            else:
                base_label = f"{provider_label}"

            key = f"{base_label}.{search_result.formats}"
            # ensure uniqueness
            i = 1
            orig = key
            while key in search_result.downloads:
                i += 1
                key = f"{orig} [{i}]"

            search_result.downloads[key] = url

    # Provider-specific resolution helpers removed. We no longer fetch provider
    # pages to avoid triggering anti-bot protections. Provider anchor hrefs are
    # exposed directly from the Anna's Archive detail page for the user/Calibre
    # to follow.

    def _get_url(self, md5):
        # Use the last working mirror if available, otherwise fall back to configured mirrors or defaults.
        base = self.working_mirror or (self.config.get('mirrors') or DEFAULT_MIRRORS)[0]
        # strip trailing slash to avoid double slashes
        return f"{base.rstrip('/')}/md5/{md5}"

    def _order_mirrors(self, mirrors, timeout=10):
        """Order mirrors by cached health then probe stale ones in parallel.

        Returns ordered list with healthy mirrors first.
        """
        meta = self.config.get('mirror_meta', {})
        now = int(time.time())

        def score(m):
            mmeta = meta.get(m, {})
            # last_good gives higher score, last_bad penalises
            last_good = mmeta.get('last_good', 0)
            last_bad = mmeta.get('last_bad', 0)
            age = now - last_good if last_good else 10**9
            return (-last_good, last_bad)

        # initial sort by score
        mirrors_sorted = sorted(mirrors, key=score)

        # probe up to 6 unknown/stale mirrors concurrently
        to_probe = [m for m in mirrors_sorted if now - meta.get(m, {}).get('last_probe', 0) > 300]
        to_probe = to_probe[:6]

        def probe(m):
            try:
                req = Request(m, method='HEAD')
                with closing(urlopen(req, timeout=timeout)) as resp:
                    code = getattr(resp, 'code', 0)
                    ok = not (500 <= code <= 599)
            except Exception:
                ok = False
            # update meta
            mm = meta.get(m, {})
            mm['last_probe'] = int(time.time())
            if ok:
                mm['last_good'] = int(time.time())
            else:
                mm['last_bad'] = int(time.time())
            meta[m] = mm
            return m, ok

        if to_probe:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(to_probe))) as ex:
                for fut in ex.map(probe, to_probe):
                    m, ok = fut
                    # nothing to do here; meta updated
                    pass

        # save meta back (do not overwrite other config keys)
        self.config['mirror_meta'] = meta
        try:
            # Persist mirror meta on the GUI thread to avoid creating Qt widgets from
            # a worker thread. If we have a GUI executor, dispatch the save there.
            if _gui_executor is not None:
                def _persist():
                    try:
                        self.save_settings(self.config_widget())
                    except Exception:
                        pass
                try:
                    _gui_executor.run.emit(_persist)
                except Exception:
                    # fallback: do not block or attempt GUI actions from worker thread
                    pass
            else:
                # No GUI executor available; skip persisting now to avoid creating widgets
                pass
        except Exception:
            # not fatal if we cannot persist now
            pass

        # final sort using last_good timestamp (recent good first)
        mirrors_final = sorted(mirrors, key=lambda m: -meta.get(m, {}).get('last_good', 0))
        return mirrors_final

    def config_widget(self):
        from calibre_plugins.store_annas_archive.config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.save_settings()

    # --- simple persistent cache helpers ---
    def _cache_path_for_url(self, url: str) -> str:
        if not self._cache_dir:
            return None
        # use sha256(url) for filename
        h = hashlib.sha256(url.encode('utf-8')).hexdigest()
        return os.path.join(self._cache_dir, h[:2], h)

    def _ensure_cache_dir_for_path(self, path: str):
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)

    def _cache_get(self, url: str):
        path = self._cache_path_for_url(url)
        if not path:
            return None
        # simple lock per file
        lock = self._cache_locks.setdefault(path, threading.Lock())
        with lock:
            if not os.path.exists(path):
                return None
            try:
                # file format: first line JSON metadata, then a blank line, then raw body
                with open(path, 'rb') as f:
                    raw = f.read()
                # split header/body
                sep = b"\n\n"
                if sep in raw:
                    hdr_raw, body = raw.split(sep, 1)
                    try:
                        meta = json.loads(hdr_raw.decode('utf-8'))
                    except Exception:
                        return body
                    # check TTL
                    if self._cache_ttl > 0:
                        ts = meta.get('ts', 0)
                        if int(time.time()) - int(ts) > self._cache_ttl:
                            try:
                                os.remove(path)
                            except Exception:
                                pass
                            return None
                    return body
                else:
                    # older format: raw body only
                    return raw
            except Exception:
                return None

    def _cache_set(self, url: str, data: bytes):
        path = self._cache_path_for_url(url)
        if not path:
            return
        self._ensure_cache_dir_for_path(path)
        lock = self._cache_locks.setdefault(path, threading.Lock())
        with lock:
            try:
                tmp = path + '.tmp'
                meta = {'ts': int(time.time()), 'url': url}
                with open(tmp, 'wb') as f:
                    f.write(json.dumps(meta).encode('utf-8'))
                    f.write(b"\n\n")
                    f.write(data)
                os.replace(tmp, path)
            except Exception:
                # best-effort caching; swallow errors
                pass
