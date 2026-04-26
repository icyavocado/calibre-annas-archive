"""Pytest conftest to inject minimal fake Calibre and Qt modules for tests.

This prevents import-time failures when importing package __init__.py which
depends on Calibre runtime modules that are not available in CI/test env.
"""
import sys
from types import ModuleType


def _inject_fake_calibre():
    if 'calibre' in sys.modules:
        return

    calibre = ModuleType('calibre')

    def browser():
        class Br:
            def open(self, url, timeout=None):
                # delegate to urllib to keep behaviour simple for tests
                from urllib.request import urlopen, Request
                return urlopen(Request(url), timeout=timeout)

        return Br()

    calibre.browser = browser
    sys.modules['calibre'] = calibre

    gui2 = ModuleType('calibre.gui2')
    gui2.open_url = lambda url: None
    sys.modules['calibre.gui2'] = gui2

    store_mod = ModuleType('calibre.gui2.store')

    class StorePlugin:
        def __init__(self, gui=None, name=None, config=None, base_plugin=None):
            self.gui = gui
            self.name = name
            self.config = config or {}
            self.working_mirror = None

        def save_settings(self, widget):
            return None

    store_mod.StorePlugin = StorePlugin
    sys.modules['calibre.gui2.store'] = store_mod

    search_result_mod = ModuleType('calibre.gui2.store.search_result')

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

    web_store_mod = ModuleType('calibre.gui2.store.web_store_dialog')

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

    customize_mod = ModuleType('calibre.customize')

    class StoreBase:
        pass

    customize_mod.StoreBase = StoreBase
    sys.modules['calibre.customize'] = customize_mod


def _inject_fake_qt():
    if 'qt.core' in sys.modules:
        return
    qt_core = ModuleType('qt.core')

    class QUrl(str):
        def __new__(cls, v):
            return str.__new__(cls, v)

    qt_core.QUrl = QUrl
    sys.modules['qt.core'] = qt_core


# inject early
_inject_fake_calibre()
_inject_fake_qt()

def _expose_package_modules():
    """Expose local package files under the 'calibre_plugins.store_annas_archive' package name

    This allows annas_archive.py to import calibre_plugins.store_annas_archive.constants during tests.
    """
    import importlib.util
    from pathlib import Path

    pkg_base = 'calibre_plugins.store_annas_archive'
    if 'calibre_plugins' not in sys.modules:
        sys.modules['calibre_plugins'] = ModuleType('calibre_plugins')
    if pkg_base not in sys.modules:
        pkg_mod = ModuleType(pkg_base)
        sys.modules[pkg_base] = pkg_mod

    root = Path(__file__).resolve().parent
    for fname in ('constants.py', 'config.py'):
        path = root / fname
        if not path.exists():
            continue
        mod_name = f"{pkg_base}.{path.stem}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            # loading may require calibre runtime for widgets; ignore failures
            pass


_expose_package_modules()
