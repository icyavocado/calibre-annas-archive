import sys
import types


def _inject_fake_calibre():
    """Inject a minimal fake 'calibre' package into sys.modules so annas_archive can be imported in tests.

    It provides the names imported by annas_archive but minimal implementations.
    """
    if 'calibre' in sys.modules:
        return

    calibre_mod = types.ModuleType('calibre')
    calibre_mod.browser = lambda: None
    sys.modules['calibre'] = calibre_mod

    gui2_mod = types.ModuleType('calibre.gui2')
    gui2_mod.open_url = lambda url: None
    sys.modules['calibre.gui2'] = gui2_mod

    store_mod = types.ModuleType('calibre.gui2.store')

    class StorePlugin:
        def __init__(self, gui, name, config=None, base_plugin=None):
            # keep a config object to be used by AnnasArchiveStore
            self.gui = gui
            self.name = name
            self.config = config or {}
            self.working_mirror = None

        def save_settings(self, widget):
            return None

    store_mod.StorePlugin = StorePlugin
    sys.modules['calibre.gui2.store'] = store_mod

    search_result_mod = types.ModuleType('calibre.gui2.store.search_result')

    class SearchResult:
        def __init__(self):
            self.formats = ''
            self.downloads = {}
            self.detail_item = None
            self.cover_url = ''
            self.title = ''
            self.author = ''
            self.price = ''
            self.drm = None

    search_result_mod.SearchResult = SearchResult
    sys.modules['calibre.gui2.store.search_result'] = search_result_mod

    web_store_mod = types.ModuleType('calibre.gui2.store.web_store_dialog')

    class WebStoreDialog:
        def __init__(self, gui, working_mirror, parent, url):
            pass

        def setWindowTitle(self, title):
            pass

        def set_tags(self, tags):
            pass

        def exec(self):
            pass

    web_store_mod.WebStoreDialog = WebStoreDialog
    sys.modules['calibre.gui2.store.web_store_dialog'] = web_store_mod


def test_get_details_resolves_provider_links():
    # prepare fake calibre modules before importing annas_archive
    _inject_fake_calibre()

    import annas_archive
    from urllib.request import Request

    # canned HTML for the md5 detail page with two provider links
    md5_html = '''
    <html>
      <body>
        <div id="md5-panel-downloads">
          <ul class="list-inside">
            <li><a class="js-download-link" href="https://libgen.li/file.php?id=107223218">Libgen.li</a></li>
            <li><a class="js-download-link" href="https://libgen.is/fiction/844087B4A2EBE1B9D4E7E52A24152306">Libgen.rs Fiction</a></li>
          </ul>
        </div>
      </body>
    </html>
    '''

    # carrier page for libgen.rs which contains a direct .epub link
    libgen_rs_html = '''
    <html>
      <body>
        <a href="/download/11cc6e0bc61151c76da8cc3231faf479.epub">Direct EPUB</a>
      </body>
    </html>
    '''

    class FakeResp:
        def __init__(self, content: bytes, url: str):
            self._content = content
            self._url = url
            self.code = 200

        def read(self):
            return self._content

        def geturl(self):
            return self._url

        def info(self):
            # minimal headers object with get_content_maintype used in code when doing HEAD
            class Info:
                def get_content_maintype(self):
                    return 'application'

            return Info()

        def close(self):
            return None

    def fake_urlopen(req, timeout=None):
        # accept either a Request object or a string
        url = getattr(req, 'full_url', None) or str(req)
        if '/md5/' in url:
            return FakeResp(md5_html.encode('utf-8'), url)
        if 'libgen.is' in url or 'libgen.rs' in url:
            return FakeResp(libgen_rs_html.encode('utf-8'), url)
        # default empty
        return FakeResp(b'', url)

    # patch annas_archive.urlopen to our fake
    annas_archive.urlopen = fake_urlopen

    # build store instance
    store = annas_archive.AnnasArchiveStore(gui=None, name='test', config={'mirrors': ['https://annas-archive.gl']}, base_plugin=None)
    store.working_mirror = 'https://annas-archive.gl'

    # create a minimal search result (no need to use calibre's SearchResult)
    class DummySearchResult:
        def __init__(self):
            self.formats = 'EPUB'
            self.downloads = {}
            self.detail_item = '11cc6e0bc61151c76da8cc3231faf479'

    sr = DummySearchResult()

    # disable strict checks in config so file.php links are accepted
    store.config['link'] = {'url_extension': False, 'content_type': False}

    store.get_details(sr, timeout=5)

    # expect both provider links to be resolved and stored
    assert any(k.startswith('Libgen.li') for k in sr.downloads.keys())
    assert any(k.startswith('Libgen.rs') for k in sr.downloads.keys())
