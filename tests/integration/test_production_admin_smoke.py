import io
import runpy
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from involta_guide.main import create_app


@pytest.mark.parametrize("hide_authenticated_header", [False, True])
def test_production_smoke_checks_real_admin_and_public_headers(
    database, monkeypatch, hide_authenticated_header
):
    settings, _ = database
    settings = settings.model_copy(update={"site_url": "https://involta.ru/guide"})
    monkeypatch.setenv("PUBLIC_URL", "https://involta.ru/guide/")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password-123456")
    visited = []
    with TestClient(create_app(settings)) as client:

        class LocalOpener:
            def open(self, request, timeout):
                headers = dict(request.header_items())
                if request.data is not None:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = client.request(
                    request.get_method(), request.full_url, content=request.data, headers=headers
                )
                assert response.status_code == 200
                visited.append(request.full_url)
                body = response.content
                if hide_authenticated_header and request.full_url.endswith("/logs"):
                    body = body.replace(b"header-admin-link", b"unavailable-link")
                result = io.BytesIO(body)
                result.status = response.status_code
                return result

        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: LocalOpener())
        script = Path(__file__).resolve().parents[2] / "deploy/check-admin.py"
        if hide_authenticated_header:
            with pytest.raises(
                AssertionError, match="Authenticated public logs header unavailable"
            ):
                runpy.run_path(str(script), run_name="__main__")
        else:
            runpy.run_path(str(script), run_name="__main__")
        assert "https://involta.ru/guide/admin/reviews-2" in visited
        assert "https://involta.ru/guide/admin/logout" in visited
        assert 'type="password"' in client.get("https://involta.ru/guide/admin").text
