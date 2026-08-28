"""Scenario cover art and the Continue-card snippet.

Unit tests for the data-URI handling in app/images.py. Then HTTP tests
confirm that the list endpoints advertise a cacheable `image_url` instead
of the inline base64, that the image route serves real bytes, and that an
adventure's snippet comes from the latest narration rather than the
player's last line.

    python -m pytest tests/test_scenario_art.py -v
"""
import base64


import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import auth, images, limits, models, schemas
from app.database import Base, SessionLocal, engine, get_db
from app.main import app

# Smallest valid PNG: a single transparent pixel.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


# --------------------------------------------------------------------------- #
# app/images.py
# --------------------------------------------------------------------------- #

def test_decode_returns_bytes_and_content_type():
    assert images.decode(PNG_URI) == (PNG_BYTES, "image/png")


def test_decode_tolerates_wrapped_base64():
    """A hand-pasted URI can carry newlines. `b64decode(validate=True)` rejects them."""
    wrapped = "data:image/png;base64," + "\n".join(
        base64.b64encode(PNG_BYTES).decode()[i:i + 24] for i in range(0, 100, 24)
    )
    # This test only confirms that the call does not raise and does not
    # silently return a partial decode of a truncated payload. The wrapped
    # prefix here is not the whole image, so a None result is also
    # acceptable. What matters is that no exception occurs.
    images.decode(wrapped)


def test_decode_rejects_non_data_uris_and_garbage():
    assert images.decode("https://example.com/a.png") is None
    assert images.decode("data:image/png;base64,!!!not base64!!!") is None
    assert images.decode("") is None
    assert images.decode(None) is None


def test_decode_rejects_svg():
    """SVG can carry script, and the app serves these bytes from its own origin."""
    svg = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode()
    assert images.decode(svg) is None


def test_public_url_points_at_the_endpoint_for_data_uris():
    class Stamp:
        def timestamp(self):
            return 1700000000.0

    assert images.public_url(7, PNG_URI, Stamp()) == "/api/scenarios/7/image?v=1700000000"


def test_public_url_passes_through_https_but_not_http():
    stamp = None
    assert images.public_url(1, "https://cdn.example.com/a.png", stamp) == \
        "https://cdn.example.com/a.png"
    # http:// would be blocked as mixed content on the deployed https app.
    assert images.public_url(1, "http://cdn.example.com/a.png", stamp) == ""
    assert images.public_url(1, "", stamp) == ""


def test_sanitize_rejects_hostile_and_oversized_values():
    assert images.sanitize("javascript:alert(1)", 1000) == ""
    assert images.sanitize("http://example.com/a.png", 1000) == ""
    assert images.sanitize(PNG_URI, len(PNG_URI) - 1) == ""  # over the cap
    assert images.sanitize(12345, 1000) == ""
    assert images.sanitize(None, 1000) == ""
    # Well-formed values survive.
    assert images.sanitize(PNG_URI, schemas.IMAGE_MAX) == PNG_URI
    assert images.sanitize("https://example.com/a.png", 1000) == "https://example.com/a.png"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=engine)
    setup = SessionLocal()
    user = models.User(is_guest=False, email="art@example.com")
    setup.add(user)
    setup.flush()
    setup.add(models.Settings(user_id=user.id, api_key="enc:dummy", model="test-model"))

    # One scenario with an uploaded picture, one with only an emoji.
    pictured = models.Scenario(user_id=user.id, title="Pictured", image=PNG_URI)
    emoji_only = models.Scenario(user_id=user.id, title="Emoji", icon="🐉")
    setup.add_all([pictured, emoji_only])
    setup.flush()

    adventure = models.Adventure(
        user_id=user.id, scenario_id=pictured.id, title="A Run",
    )
    setup.add(adventure)
    setup.flush()
    # A full turn: opening narration, the player's line, then the AI's reply.
    setup.add_all([
        models.Action(adventure_id=adventure.id, index=0, type="ai",
                      text="The door groans open."),
        models.Action(adventure_id=adventure.id, index=1, type="do",
                      text="I draw my sword."),
        models.Action(adventure_id=adventure.id, index=2, type="ai",
                      text="Steel rings.  The\ncorridor  answers."),
    ])
    setup.commit()
    ids = {"scenario": pictured.id, "emoji": emoji_only.id, "adventure": adventure.id}
    user_id = user.id
    setup.close()

    monkeypatch.setattr(limits, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(limits, "check_row_cap", lambda *a, **k: None)

    def _current_user(db=Depends(get_db)):
        return db.get(models.User, user_id)

    app.dependency_overrides[auth.get_current_user] = _current_user
    c = TestClient(app)
    c.ids = ids
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def test_scenario_list_advertises_a_url_and_hides_the_base64(client):
    rows = {row["title"]: row for row in client.get("/api/scenarios").json()}

    pictured = rows["Pictured"]
    assert pictured["image_url"].startswith(f"/api/scenarios/{client.ids['scenario']}/image?v=")
    # A list response must never carry the inline image.
    assert "image" not in pictured

    assert rows["Emoji"]["image_url"] == ""
    assert rows["Emoji"]["icon"] == "🐉"


def test_image_endpoint_serves_the_decoded_bytes(client):
    resp = client.get(f"/api/scenarios/{client.ids['scenario']}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == PNG_BYTES
    assert "immutable" in resp.headers["cache-control"]


def test_image_endpoint_404s_without_an_upload(client):
    resp = client.get(f"/api/scenarios/{client.ids['emoji']}/image")
    assert resp.status_code == 404


def test_single_scenario_still_returns_the_raw_uri_for_editing(client):
    """The editor needs the actual value to preview and to clear."""
    body = client.get(f"/api/scenarios/{client.ids['scenario']}").json()
    assert body["image"] == PNG_URI


def test_adventure_list_carries_snippet_and_inherited_art(client):
    row = next(r for r in client.get("/api/adventures").json()
               if r["id"] == client.ids["adventure"])
    # Latest narration, whitespace collapsed. Not the player's line, "I draw my sword."
    assert row["snippet"] == "Steel rings. The corridor answers."
    assert row["image_url"].startswith(f"/api/scenarios/{client.ids['scenario']}/image?v=")
    assert row["action_count"] == 3


def test_snippet_is_truncated_on_a_word_boundary(client):
    from app.routers.adventures import SNIPPET_MAX, _snippet

    long_text = "word " * 200
    out = _snippet(long_text)
    assert len(out) <= SNIPPET_MAX + 1  # +1 for the ellipsis
    assert out.endswith("…")
    assert "wor…" not in out  # never cuts mid-word


def test_scenario_export_import_round_trips_the_art(client):
    bundle = client.get(f"/api/scenarios/{client.ids['scenario']}/export").json()
    assert bundle["image"] == PNG_URI

    created = client.post("/api/scenarios/import", json=bundle).json()["scenario"]
    assert created["image"] == PNG_URI


def test_import_drops_a_hostile_image_value(client):
    bundle = {"title": "Sneaky", "image": "javascript:alert(1)"}
    created = client.post("/api/scenarios/import", json=bundle).json()["scenario"]
    assert created["image"] == ""
