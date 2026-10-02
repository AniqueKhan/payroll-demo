from .base import Importer
from .store_a import StoreAImporter
from .store_b import StoreBImporter
from .store_c import StoreCImporter

IMPORTERS: dict[str, type[Importer]] = {
    cls.format_name: cls for cls in (StoreAImporter, StoreBImporter, StoreCImporter)
}


def get_importer(export_format: str) -> type[Importer]:
    return IMPORTERS[export_format]
