from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models._common import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.inventory_unit import InventoryUnit


class ProductKind(str, enum.Enum):
    SINGLE = "single"
    SEALED = "sealed"
    SUPPLY = "supply"


class Product(Base, TimestampMixin):
    __tablename__ = "product"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('single','sealed','supply')",
            name="product_kind_check",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    tcgplayer_product_id: Mapped[int | None] = mapped_column(unique=True)
    # Marketplace product page id — distinct from tcgplayer_product_id (a
    # SKU-level id from the seller export). Resolved lazily via the
    # marketplace search API and cached here. Drives image URL + product
    # page URL.
    marketplace_product_id: Mapped[int | None] = mapped_column(index=True)
    shopify_product_id: Mapped[int | None] = mapped_column(unique=True)
    shopify_variant_id: Mapped[int | None] = mapped_column()

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    set: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # True iff the JPEG has been downloaded to the deterministic local
    # path (data/images/<set-slug>/<name-slug>__<number-slug>.jpg).
    # Templates check this; the path itself is computed via
    # ``image_url_path`` from set/name/number — no DB lookup needed.
    has_image: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), default="English")
    is_foil: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    rarity: Mapped[str | None] = mapped_column(String(64))
    card_type: Mapped[str | None] = mapped_column(String(64))
    sealed_subtype: Mapped[str | None] = mapped_column(String(64))
    supply_category: Mapped[str | None] = mapped_column(String(64))
    # Collector number (e.g. "35/82"). Combined with set + name, this is
    # how image filenames are derived — see app.sync.tcgplayer.image_paths.
    number: Mapped[str | None] = mapped_column(String(32))

    is_online_listable: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    inventory_units: Mapped[list["InventoryUnit"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )

    @property
    def image_url_path(self) -> str | None:
        """URL the static `/images/` mount serves. ``None`` when the
        image hasn't been fetched yet — templates should branch on
        ``has_image`` to decide whether to render an <img>."""
        if not self.has_image:
            return None
        from app.sync.tcgplayer.image_paths import image_url_path

        return image_url_path(set_name=self.set, name=self.name, number=self.number)

    @property
    def image_local_path(self):
        """Filesystem Path under data/images/. Used by eBay outbound
        upload + manual debugging."""
        if not self.has_image:
            return None
        from app.sync.tcgplayer.image_paths import image_local_path

        return image_local_path(
            set_name=self.set, name=self.name, number=self.number
        )

    def __repr__(self) -> str:
        return f"<Product id={self.id} kind={self.kind} name={self.name!r}>"
