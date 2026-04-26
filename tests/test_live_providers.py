import os
import pytest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from lxml import html

from annas_archive import AnnasArchiveStore, urljoin


if os.environ.get('RUN_NETWORK_TESTS', '').lower() not in ('1', 'true', 'yes'):
    pytest.skip('Network tests disabled. Set RUN_NETWORK_TESTS=true to enable.', allow_module_level=True)


def _open(url, timeout=15):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    return urlopen(req, timeout=timeout)


def test_live_provider_resolution():
    md5 = '11cc6e0bc61151c76da8cc3231faf479'
    base = f'https://annas-archive.gl/md5/{md5}'
    try:
        resp = _open(base, timeout=20)
    except HTTPError as e:
        if 500 <= e.code <= 599:
            pytest.skip(f'Anna\'s Archive returned {e.code}, skipping live provider tests')
        raise

    doc = html.fromstring(resp.read())
    links = doc.xpath('//div[@id="md5-panel-downloads"]//a[@href]')
    assert links, 'No download anchors found on md5 detail page'

    found_any = False
    # check a small sample of provider links
    for a in links[:12]:
        href = a.get('href')
        text = ''.join(a.itertext()).strip().lower()
        abs_href = urljoin(base, href)

        # internal fast/slow partner endpoints — ensure they exist/respond
        if any(k in abs_href for k in ('/fast_download/', '/slow_download/')):
            try:
                r = _open(abs_href, timeout=10)
                # If we got a response, consider it resolved (service may redirect)
                if getattr(r, 'status', None) in (200, 302, 303, 307, None):
                    found_any = True
            except HTTPError as e:
                # 5xx means temporary, skip this provider but don't fail whole test
                if 500 <= e.code <= 599:
                    continue
                continue
            except Exception:
                continue

        # Z-Library links
        if 'z-lib' in abs_href or 'z-library' in text:
            try:
                br = type('B', (), {'open': lambda self, u, timeout=None: urlopen(Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=timeout)})()
                res = AnnasArchiveStore._get_zlib_link(abs_href, br)
                if res:
                    found_any = True
            except HTTPError as e:
                if 500 <= e.code <= 599:
                    pytest.skip('Z-Library temporarily unavailable')
                continue
            except Exception:
                continue

        # LibGen variants
        if 'libgen' in abs_href or 'libgen' in text:
            try:
                br = type('B', (), {'open': lambda self, u, timeout=None: urlopen(Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=timeout)})()
                res = AnnasArchiveStore._get_libgen_link(abs_href, br)
                if res:
                    found_any = True
            except HTTPError as e:
                if 500 <= e.code <= 599:
                    pytest.skip('LibGen provider temporarily unavailable')
                continue
            except Exception:
                continue

    assert found_any, 'No provider links resolved in live test'
