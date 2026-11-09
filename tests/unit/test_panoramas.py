from involta_guide.web import panoramas


def test_new_image_is_discovered_without_registry_or_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(panoramas, "PANORAMA_DIRECTORY", tmp_path)
    assert panoramas.panorama_for("new-place") is None
    (tmp_path / "new-place.avif").write_bytes(b"image")
    assert panoramas.panorama_for("new-place") == "/assets/panoramas/new-place.avif"
    (tmp_path / "new-place.avif").unlink()
    assert panoramas.panorama_for("new-place") is None
    (tmp_path / "directory.avif").mkdir()
    assert panoramas.panorama_for("directory") is None


def test_image_lookup_cannot_escape_the_panorama_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(panoramas, "PANORAMA_DIRECTORY", tmp_path / "images")
    (tmp_path / "secret.avif").write_bytes(b"private")
    for slug in ["../secret", "/secret", "..", "ples.avif", "ples/other", "плёс", ""]:
        assert panoramas.panorama_for(slug) is None


def test_only_avif_panoramas_are_versioned():
    assert panoramas.panorama_for("ples") == "/assets/panoramas/ples.avif"
    images = list(panoramas.PANORAMA_DIRECTORY.iterdir())
    assert images
    for image in images:
        assert image.is_file() and image.suffix == ".avif", image
        header = image.read_bytes()[:32]
        assert header[4:8] == b"ftyp" and b"avif" in header[8:], image
