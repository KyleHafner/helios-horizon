from fastapi.testclient import TestClient

from game_control.web_main import create_app


async def _unavailable_rpc(_request):
    raise RuntimeError("controller unavailable")


def test_security_headers_and_api_no_store_are_fail_closed():
    app = create_app(
        rpc=_unavailable_rpc,
        proxy_credential="synthetic-proxy-credential",
        session_db=":memory:",
    )

    with TestClient(app, base_url="https://games.example.com") as client:
        response = client.get("/api/v1/session")

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
