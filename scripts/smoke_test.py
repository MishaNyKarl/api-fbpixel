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

        duplicate = client.post(
            "/api/pixel/track",
            json=payload,
            headers={"x-api-key": "test-api-key"},
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["duplicate"] is True

        print("smoke ok")
    finally:
        main.engine.dispose()
        tmpdir.cleanup()


if __name__ == "__main__":
    main_test()
