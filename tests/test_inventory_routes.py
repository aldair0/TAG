"""Edit-item page + manual product picture upload.

Covers the regression where the edit page 500'd (collection joinedload
without ``.unique()``) and the new manual-image upload/remove flow.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import InventoryUnit, Product
from app.sync.tcgplayer.image_paths import image_local_path


def _seed_unit(session, *, name="Manual Card", set_name="Test Set", number="1/100"):
    p = Product(
        tcgplayer_product_id=None,  # store item
        kind="single",
        name=name,
        set=set_name,
        number=number,
        is_online_listable=True,
        has_image=False,
    )
    session.add(p)
    session.flush()
    u = InventoryUnit(
        product_id=p.id,
        condition="NM",
        quantity_on_hand=2,
        unit_price=Decimal("5.00"),
    )
    session.add(u)
    session.commit()
    return u


# ---- edit page no longer 500s -----------------------------------------


def test_edit_page_renders(client, session):
    """Regression: the edit form joinedloads the adjustments collection,
    which needs .unique() — previously raised InvalidRequestError → 500."""
    unit = _seed_unit(session)
    r = client.get(f"/admin/inventory/{unit.id}/edit")
    assert r.status_code == 200
    assert "Edit item" in r.text
    assert "Picture" in r.text  # new upload card present


def test_edit_page_404_for_missing_unit(client):
    assert client.get("/admin/inventory/999999/edit").status_code == 404


# ---- manual picture upload --------------------------------------------


def test_upload_image_saves_file_and_sets_flag(client, session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.sync.tcgplayer.image_paths.IMAGES_ROOT", tmp_path)
    unit = _seed_unit(session)

    r = client.post(
        f"/admin/inventory/{unit.id}/image",
        files={"image": ("card.png", b"\x89PNG\r\n\x1a\nfake-bytes", "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "image_saved=1" in r.headers["location"]

    session.expire_all()
    product = session.get(Product, unit.product_id)
    assert product.has_image is True

    path = image_local_path(
        set_name=product.set, name=product.name, number=product.number, root=tmp_path
    )
    assert path.exists()
    assert path.read_bytes().startswith(b"\x89PNG")


def test_upload_rejects_non_image(client, session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.sync.tcgplayer.image_paths.IMAGES_ROOT", tmp_path)
    unit = _seed_unit(session)

    r = client.post(
        f"/admin/inventory/{unit.id}/image",
        files={"image": ("notes.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=bad_image" in r.headers["location"]

    session.expire_all()
    assert session.get(Product, unit.product_id).has_image is False


def test_upload_rejects_too_large(client, session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.sync.tcgplayer.image_paths.IMAGES_ROOT", tmp_path)
    import app.routes.inventory as inv

    monkeypatch.setattr(inv, "MAX_IMAGE_BYTES", 10)
    unit = _seed_unit(session)

    r = client.post(
        f"/admin/inventory/{unit.id}/image",
        files={"image": ("big.png", b"\x89PNG" + b"x" * 50, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=image_too_large" in r.headers["location"]
    session.expire_all()
    assert session.get(Product, unit.product_id).has_image is False


def test_delete_image_clears_flag_and_file(client, session, tmp_path, monkeypatch):
    monkeypatch.setattr("app.sync.tcgplayer.image_paths.IMAGES_ROOT", tmp_path)
    unit = _seed_unit(session)

    # Upload, then delete.
    client.post(
        f"/admin/inventory/{unit.id}/image",
        files={"image": ("card.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg")},
        follow_redirects=False,
    )
    product = session.get(Product, unit.product_id)
    path = image_local_path(
        set_name=product.set, name=product.name, number=product.number, root=tmp_path
    )
    assert path.exists()

    r = client.post(
        f"/admin/inventory/{unit.id}/image/delete", follow_redirects=False
    )
    assert r.status_code == 303
    assert "image_removed=1" in r.headers["location"]

    session.expire_all()
    assert session.get(Product, unit.product_id).has_image is False
    assert not path.exists()
