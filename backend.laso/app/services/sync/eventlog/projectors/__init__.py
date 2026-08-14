"""
Concrete projectors — one per aggregate type.

Importing this package registers every projector against
:class:`~app.services.sync.eventlog.projector.ProjectorRegistry`. The
push-endpoint bootstrap imports this package once at startup so the
registry is populated before the first event lands.
"""

from app.services.sync.eventlog.projectors.customer import CustomerProjector
from app.services.sync.eventlog.projectors.sale import SaleProjector
from app.services.sync.eventlog.projectors.prescription import PrescriptionProjector
from app.services.sync.eventlog.projectors.stock import StockProjector
from app.services.sync.eventlog.projectors.stock_transfer import StockTransferProjector
from app.services.sync.eventlog.projectors.drug import DrugProjector, DrugCategoryProjector

__all__ = [
    "CustomerProjector",
    "SaleProjector",
    "PrescriptionProjector",
    "StockProjector",
    "StockTransferProjector",
    "DrugProjector",
    "DrugCategoryProjector",
]
