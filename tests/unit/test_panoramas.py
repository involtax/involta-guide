import struct

from involta_guide.web import panoramas


def test_new_image_is_discovered_without_registry_or_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(panoramas, "PANORAMA_DIRECTORY", tmp_path)
    assert panoramas.panorama_for("new-place") is None
    (tmp_path / "new-place.png").write_bytes(b"image")
    assert panoramas.panorama_for("new-place") == "/assets/panoramas/new-place.png"
    (tmp_path / "new-place.png").unlink()
    assert panoramas.panorama_for("new-place") is None
    (tmp_path / "directory.png").mkdir()
    assert panoramas.panorama_for("directory") is None


def test_image_lookup_cannot_escape_the_panorama_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(panoramas, "PANORAMA_DIRECTORY", tmp_path / "images")
    (tmp_path / "secret.png").write_bytes(b"private")
    for slug in ["../secret", "/secret", "..", "ples.png", "ples/other", "плёс", ""]:
        assert panoramas.panorama_for(slug) is None


def test_versioned_panorama_dimensions_match_the_reserved_layout():
    assert panoramas.panorama_for("ples") == "/assets/panoramas/ples.png"
    for image in panoramas.PANORAMA_DIRECTORY.glob("*.png"):
        header = image.read_bytes()[:33]
        assert header[:8] == b"\x89PNG\r\n\x1a\n", image
        assert header[12:16] == b"IHDR", image
        assert struct.unpack(">II", header[16:24]) == (2172, 724), image
        assert header[24:26] == bytes([8, 6]), f"{image}: expected 8-bit RGBA"
