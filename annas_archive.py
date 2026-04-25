from contextlib import closing
from http.client import RemoteDisconnected
from math import ceil
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import urlopen, Request
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

SearchResults = Generator[SearchResult, None, None]


class AnnasArchiveStore(StorePlugin):

    def __init__(self, gui, name, config=None, base_plugin=None):
        super().__init__(gui, name, config, base_plugin)
        self.working_mirror = None

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

        # fetch detail page
        with closing(urlopen(Request(self._get_url(search_result.detail_item)), timeout=timeout)) as f:
            doc = html.fromstring(f.read())

        links = doc.xpath('//div[@id="md5-panel-downloads"]/ul[contains(@class, "list-inside")]/li/a[contains(@class, "js-download-link")]')

        def resolve_provider(link):
            url = link.get('href')
            link_text = ''.join(link.itertext())
            if not url:
                return link_text, None

            try:
                if link_text == 'Libgen.li':
                    with closing(urlopen(Request(url), timeout=timeout)) as resp:
                        doc2 = html.fromstring(resp.read())
                        scheme, _, host, _ = resp.geturl().split('/', 3)
                    found = ''.join(doc2.xpath('//a[h2[text()="GET"]]/@href'))
                    return link_text, (f"{scheme}//{host}/{found}" if found else None)
                if link_text == 'Libgen.rs Fiction' or link_text == 'Libgen.rs Non-Fiction':
                    with closing(urlopen(Request(url), timeout=timeout)) as resp:
                        doc2 = html.fromstring(resp.read())
                    found = ''.join(doc2.xpath('//h2/a[text()="GET"]/@href'))
                    return link_text, (found if found else None)
                if link_text.startswith('Sci-Hub'):
                    with closing(urlopen(Request(url), timeout=timeout)) as resp:
                        doc2 = html.fromstring(resp.read())
                        scheme, _ = resp.geturl().split('/', 1)
                    found = ''.join(doc2.xpath('//embed[@id="pdf"]/@src'))
                    return link_text, (scheme + found if found else None)
                if link_text == 'Z-Library':
                    with closing(urlopen(Request(url), timeout=timeout)) as resp:
                        doc2 = html.fromstring(resp.read())
                        scheme, _, host, _ = resp.geturl().split('/', 3)
                    found = ''.join(doc2.xpath('//a[contains(@class, "addDownloadedBook")]/@href'))
                    return link_text, (f"{scheme}//{host}/{found}" if found else None)
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
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[h2[text()="GET"]]/@href'))
        return f"{scheme}//{host}/{url}"

    @staticmethod
    def _get_libgen_nonfiction_link(url: str, br) -> str:
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
        url = ''.join(doc.xpath('//h2/a[text()="GET"]/@href'))
        return url

    @staticmethod
    def _get_scihub_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _ = resp.geturl().split('/', 1)
        url = ''.join(doc.xpath('//embed[@id="pdf"]/@src'))
        if url:
            return scheme + url

    @staticmethod
    def _get_zlib_link(url, br):
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[contains(@class, "addDownloadedBook")]/@href'))
        if url:
            return f"{scheme}//{host}/{url}"

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
