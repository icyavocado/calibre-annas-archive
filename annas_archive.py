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

from calibre import browser
from calibre.gui2 import open_url
from calibre.gui2.store import StorePlugin
from calibre.gui2.store.search_result import SearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog
from calibre_plugins.store_annas_archive.constants import DEFAULT_MIRRORS, RESULTS_PER_PAGE, SearchOption
from lxml import html

try:
    from qt.core import QUrl
except (ImportError, ModuleNotFoundError):
    from PyQt5.Qt import QUrl

# expose a module-level urlopen alias so tests can patch annas_archive.urlopen
urlopen = _raw_urlopen
SearchResults = Generator[SearchResult, None, None]


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

    def _search(self, url: str, max_results: int, timeout: int) -> SearchResults:
        br = browser()
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
                    with closing(br.open(url.format(base=mirror, page=page), timeout=timeout)) as resp:
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
                s = SearchResult()

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
        if not search_result.formats:
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

        def find_download_in_doc(doc2, base2):
            # prefer explicit links that end with expected format
            for a in doc2.xpath('//a[@href]'):
                href = a.get('href')
                if not href:
                    continue
                full = urljoin(base2, href)
                if full.split('?', 1)[0].lower().endswith(_format):
                    return full

            # prefer anchors with download/get text
            for a in doc2.xpath('//a'):
                text = ''.join(a.itertext()).strip().lower()
                if 'get' in text or 'download' in text or 'direct' in text:
                    href = a.get('href')
                    if href:
                        return urljoin(base2, href)

            # forms with action
            for form in doc2.xpath('//form[@action]'):
                action = form.get('action')
                if action:
                    return urljoin(base2, action)

            # meta refresh
            meta = doc2.xpath('//meta[@http-equiv="refresh"]/@content')
            if meta:
                import re
                m = re.search(r'url=(.*)', meta[0], re.I)
                if m:
                    return urljoin(base2, m.group(1).strip(' "\''))
            return None

        def resolve_provider(link):
            href = link.get('href')
            link_text = ''.join(link.itertext()).strip()
            if not href:
                return link_text, None

            abs_href = urljoin(base_page_url, href)

            # quick accept: if link already points to a file or known download path
            p = urlparse(abs_href)
            # accept direct partner fast/slow download endpoints, libgen file.php or obvious download endpoints
            if any(fragment in abs_href for fragment in ('file.php', 'ads.php', '/download', '/fast_download', '/slow_download', '/fast_download_not_member')):
                return link_text, abs_href
            if abs_href.split('?', 1)[0].lower().endswith(_format):
                return link_text, abs_href

            try:
                # use cache for provider pages too when enabled
                if self._cache_dir:
                    raw = self._cache_get(abs_href)
                    if raw is not None:
                        doc2 = html.fromstring(raw)
                        base2 = abs_href
                    else:
                        with closing(urlopen(Request(abs_href), timeout=timeout)) as resp:
                            raw = resp.read()
                            base2 = resp.geturl()
                        doc2 = html.fromstring(raw)
                        self._cache_set(abs_href, raw)
                else:
                    with closing(urlopen(Request(abs_href), timeout=timeout)) as resp:
                        doc2 = html.fromstring(resp.read())
                        base2 = resp.geturl()

                    # first, try to find explicit download link
                    found = find_download_in_doc(doc2, base2)
                    if found:
                        return link_text, found

                    # fallback: if provider page redirects to a direct file
                    if base2.split('?', 1)[0].lower().endswith(_format):
                        return link_text, base2
            except Exception:
                return link_text, None

            return link_text, None

        # resolve provider links concurrently
        resolved = {}
        if links:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(links))) as ex:
                futures = {ex.submit(resolve_provider, link): link for link in links}
                for fut in concurrent.futures.as_completed(futures):
                    link_text, resolved_url = fut.result()
                    if resolved_url:
                        resolved[link_text] = resolved_url

        for link_text, url in resolved.items():
            if not url:
                continue

            # Takes longer, but more accurate
            if content_type:
                try:
                    with urlopen(Request(url, method='HEAD'), timeout=timeout) as resp:
                        if resp.info().get_content_maintype() != 'application':
                            continue
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected):
                    # if HEAD fails, skip content-type check and fall back to extension check if enabled
                    if not url_extension:
                        continue
            if url_extension and not content_type:
                # Speeds it up by checking the extension of the url. Strip query params first.
                path = url.split('?', 1)[0]
                if not path.lower().endswith(_format):
                    continue

            search_result.downloads[f"{link_text}.{search_result.formats}"] = url

    @staticmethod
    def _get_libgen_link(url: str, br) -> str:
        # Libgen pages can be various: try to prefer anchors with explicit GET or direct file links
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            base = resp.geturl()

        # prefer anchors with class/js-download or with text 'GET' or direct file.php
        candidates = doc.xpath('//a[contains(@class, "download") or contains(@class, "js-download") or contains(text(), "GET")]/@href')
        if not candidates:
            # fallback to links that include file.php or download
            candidates = doc.xpath('//a[contains(@href, "file.php") or contains(@href, "download")]/@href')
        if candidates:
            href = candidates[0]
            return urljoin(base, href)
        return None

    @staticmethod
    def _get_libgen_nonfiction_link(url: str, br) -> str:
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            base = resp.geturl()

        candidates = doc.xpath('//a[contains(@class, "js-download") or contains(text(), "GET")]/@href')
        if not candidates:
            candidates = doc.xpath('//h2/a[text()="GET"]/@href')
        if candidates:
            return urljoin(base, candidates[0])
        return None

    @staticmethod
    def _get_scihub_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            base = resp.geturl()

        # Sci-Hub sometimes embeds the PDF or provides a direct link
        src = ''.join(doc.xpath('//embed[@id="pdf"]/@src | //iframe[contains(@src, "pdf")]/@src | //a[contains(@href, ".pdf")]/@href'))
        if src:
            return urljoin(base, src)
        return None

    @staticmethod
    def _get_zlib_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            base = resp.geturl()

        # prefer anchors that add the book or direct download links
        candidates = doc.xpath('//a[contains(@class, "addDownloadedBook") or contains(@class, "download") or contains(@href, "download")]/@href')
        if candidates:
            return urljoin(base, candidates[0])
        # sometimes z-lib has a link with md5 param
        candidates = doc.xpath('//a[contains(@href, "md5=")]/@href')
        if candidates:
            return urljoin(base, candidates[0])
        return None

    def _get_url(self, md5):
        return f"{self.working_mirror}/md5/{md5}"

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
            # attempt to save config if underlying store supports it
            self.save_settings(self.config_widget())
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
