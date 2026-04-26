from calibre.customize import StoreBase

# Module-level version used by CI build scripts to name the release zip.
# Keep in-sync with the class attribute below.
version = (0, 2, 6)


class AnnasArchiveStore(StoreBase):
    name                = 'Anna\'s Archive'
    description         = 'The world\'s largest open-source open-data library.'
    supported_platforms = ['windows', 'osx', 'linux']
    author              = 'ScottBot10'
    version             = (0, 2, 6)
    minimum_calibre_version = (5, 0, 0)
    formats             = ['EPUB', 'MOBI', 'PDF', 'AZW3', 'CBR', 'CBZ', 'FB2']
    drm_free_only       = True

    actual_plugin = 'calibre_plugins.store_annas_archive.annas_archive:AnnasArchiveStore'

    def is_customizable(self):
        return True
