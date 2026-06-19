import os
import sys
import tempfile
from pathlib import Path


tmpdir = tempfile.TemporaryDirectory()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["DB_PATH"] = os.path.join(tmpdir.name, "api-pixel-test.db")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["API_PUBLIC_KEY"] = "test-api-key"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["DEDUP_TTL_SECONDS"] = "600"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.lists = {}

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start : end + 1]
        return True

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]


async def fake_send_to_meta(event, meta_pixel_id, meta_access_token, pixel=None, context=None):
    return {
        "status_code": 200,
        "fbtrace_id": "fake-trace",
        "events_received": 1,
        "messages": [],
    }


def create_pixel():
    db = main.SessionLocal()
    try:
        pixel = main.Pixel(
            public_id="px_test",
            name="Test Pixel",
            buyer_name="admin",
            meta_pixel_id="123456789",
            meta_access_token="TEST_TOKEN",
            allowed_domains="example.com",
            is_active=True,
        )
        db.add(pixel)
        db.commit()
    finally:
        db.close()


def main_test():
    try:
        main.rds = FakeRedis()
        main.send_to_meta = fake_send_to_meta
        create_pixel()

        client = TestClient(main.app)

        health = client.get("/healthz")
        assert health.status_code == 200, health.text

        admin = client.get("/admin/pixels", auth=("admin", "admin"))
        assert admin.status_code == 200, admin.text
        assert "px_test" in admin.text
        assert "pixel-search-input" in admin.text
        assert "pixel-column-settings" in admin.text
        assert "theme-toggle" in admin.text

        users = client.get("/admin/users", auth=("admin", "admin"))
        assert users.status_code == 200, users.text
        assert "admin" in users.text

        created_user = client.post(
            "/admin/users/new",
            data={
                "username": "buyer1",
                "password": "buyer-pass",
                "buyer_name": "admin",
                "role": "buyer",
                "is_active": "on",
            },
            auth=("admin", "admin"),
            follow_redirects=False,
        )
        assert created_user.status_code == 303, created_user.text

        buyer_logs = client.get("/admin/logs", auth=("buyer1", "buyer-pass"))
        assert buyer_logs.status_code == 200, buyer_logs.text

        quality = client.get("/admin/quality", auth=("admin", "admin"))
        assert quality.status_code == 200, quality.text
        assert "Tracking Quality" in quality.text

        diagnostics_empty = client.get("/admin/diagnostics", auth=("admin", "admin"))
        assert diagnostics_empty.status_code == 200, diagnostics_empty.text
        assert "Click Diagnostics" in diagnostics_empty.text

        buyer_users = client.get("/admin/users", auth=("buyer1", "buyer-pass"))
        assert buyer_users.status_code == 403, buyer_users.text

        payload = {
            "tracker_pixel_id": "px_test",
            "event_name": "Lead",
            "clickid": "smoke_click_1",
            "fbclid": "smoke_fbclid",
            "event_source_url": "https://example.com/",
            "user_data_raw": {"phone": "+79990000000"},
        }
        lead = client.post(
            "/api/pixel/track",
            json=payload,
            headers={"x-api-key": "test-api-key"},
        )
        assert lead.status_code == 200, lead.text
        assert lead.json()["ok"] is True
        assert lead.json()["event_id"] == "smoke_click_1:Lead"

        main.log_meta_to_redis({
            "event_name": "Lead",
            "event_id": "smoke_click_1:Lead",
            "clickid": "smoke_click_1",
            "fbclid": "smoke_fbclid",
            "fbc": "fb.1.123.smoke_fbclid",
            "tracker_pixel_id": "px_test",
            "buyer_name": "admin",
            "pixel_name": "Test Pixel",
            "status_code": 200,
        })
        diagnostics = client.get("/admin/diagnostics?buyer=__all__&clickid=smoke_click_1", auth=("admin", "admin"))
        assert diagnostics.status_code == 200, diagnostics.text
        assert "smoke_click_1:Lead" in diagnostics.text

        duplicate = client.post(
            "/api/pixel/track",
            json=payload,
            headers={"x-api-key": "test-api-key"},
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["duplicate"] is True

        rejected_payload = dict(payload)
        rejected_payload["tracker_pixel_id"] = "px_missing"
        rejected_payload["clickid"] = "smoke_missing_pixel"
        rejected = client.post(
            "/api/pixel/track",
            json=rejected_payload,
            headers={"x-api-key": "test-api-key"},
        )
        assert rejected.status_code == 400, rejected.text

        rejected_logs = client.get(
            "/admin/logs?buyer=__all__&clickid=smoke_missing_pixel",
            auth=("admin", "admin"),
        )
        assert rejected_logs.status_code == 200, rejected_logs.text
        assert "smoke_missing_pixel" in rejected_logs.text
        assert "rejected before Meta" in rejected_logs.text

        today = main.app_now().date().isoformat()
        dated_logs = client.get(
            f"/admin/logs?buyer=__all__&date_from={today}&date_to={today}&clickid=smoke_missing_pixel",
            auth=("admin", "admin"),
        )
        assert dated_logs.status_code == 200, dated_logs.text
        assert "smoke_missing_pixel" in dated_logs.text

        main.log_meta_to_redis({
            "event_name": "Lead",
            "clickid": "older_lead_click",
            "tracker_pixel_id": "px_test",
            "buyer_name": "admin",
            "status_code": 200,
        })
        for i in range(150):
            main.log_meta_to_redis({
                "event_name": "PageView",
                "clickid": f"pageview_click_{i}",
                "tracker_pixel_id": "px_test",
                "buyer_name": "admin",
                "status_code": 200,
            })
        lead_logs = client.get(
            "/admin/logs?buyer=__all__&event=Lead&clickid=",
            auth=("admin", "admin"),
        )
        assert lead_logs.status_code == 200, lead_logs.text
        assert "older_lead_click" in lead_logs.text
        assert "pageview_click_149" not in lead_logs.text

        print("smoke ok")
    finally:
        main.engine.dispose()
        tmpdir.cleanup()


if __name__ == "__main__":
    main_test()
