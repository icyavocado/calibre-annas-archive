from pathlib import Path


class FakeResp:
    def __init__(self, path: Path, url: str):
        self._path = path
        self._url = url
        self.code = 200

    def read(self):
        return self._path.read_bytes()

    def geturl(self):
        return self._url

    def info(self):
        class Info:
            def get_content_maintype(self):
                return 'application'

        return Info()

    def close(self):
        return None


class FakeBrowser:
    def __init__(self, mapping):
        self.mapping = mapping

    def open(self, url, timeout=None):
        # choose fixture by hostname path
        for key, path in self.mapping.items():
            if key in url:
                return FakeResp(Path(path), url)
        return FakeResp(Path('tests/fixtures/md5_all_providers.html'), url)


def test_libgen_li_helper():
    import annas_archive
    br = FakeBrowser({'libgen.li': 'tests/fixtures/libgen_li.html'})
    result = annas_archive.AnnasArchiveStore._get_libgen_link('https://libgen.li/ads.php?md5=abc', br)
    assert result is not None
    assert 'book.php' in result or 'file.php' in result


def test_libgen_rs_helper():
    import annas_archive
    br = FakeBrowser({'libgen.is': 'tests/fixtures/libgen_rs.html'})
    result = annas_archive.AnnasArchiveStore._get_libgen_link('https://libgen.is/fiction/84408', br)
    assert result is not None
    assert 'download' in result


def test_zlib_helper():
    import annas_archive
    br = FakeBrowser({'z-lib.org': 'tests/fixtures/zlib.html'})
    result = annas_archive.AnnasArchiveStore._get_zlib_link('https://z-lib.org/book/index.php?md5=abcd', br)
    assert result is not None
    assert 'download' in result or 'md5=' in result


def test_scihub_helper():
    import annas_archive
    br = FakeBrowser({'sci-hub.se': 'tests/fixtures/scihub.html'})
    result = annas_archive.AnnasArchiveStore._get_scihub_link('https://sci-hub.se/10.1000/xyz', br)
    assert result is not None
    assert result.endswith('.pdf') or 'downloads' in result
