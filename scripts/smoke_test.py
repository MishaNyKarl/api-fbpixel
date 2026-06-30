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
os.environ["WHALE_TIKTOK_SECRET"] = "whale-secret"

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


async def fake_send_to_tiktok(payload, pixel, context):
    fake_send_to_tiktok.calls.append((payload, pixel, context))
    return {
        "status_code": 200,
        "code": 0,
        "message": "OK",
        "request_id": "fake-tiktok-request",
        "body": {"code": 0, "message": "OK", "request_id": "fake-tiktok-request"},
    }


fake_send_to_tiktok.calls = []


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
        other_pixel = main.Pixel(
            public_id="px_other",
            name="Other Buyer Pixel",
            buyer_name="other-buyer",
            meta_pixel_id="987654321",
            meta_access_token="OTHER_TOKEN",
            allowed_domains="other.example.com",
            is_active=True,
        )
        tiktok_pixel = main.TikTokPixel(
            public_id="D75QFE3C77UDH74CJM70",
            name="TikTok Whale Pixel",
            buyer_name="admin",
            dataset_id="D75QFE3C77UDH74CJM70",
            access_token="TT_TOKEN",
            event_name="CompletePayment",
            currency="USD",
            allowed_statuses="Approved,Paid",
            flow_ids="flow-main",
            send_without_ttclid=True,
            is_active=True,
        )
        other_tiktok_pixel = main.TikTokPixel(
            public_id="tt_other",
            name="Other TikTok Pixel",
            buyer_name="other-buyer",
            dataset_id="OTHER_DATASET",
            access_token="OTHER_TT_TOKEN",
            event_name="CompletePayment",
            currency="USD",
            allowed_statuses="Approved,Paid",
            is_active=True,
        )
        db.add(pixel)
        db.add(other_pixel)
        db.add(tiktok_pixel)
        db.add(other_tiktok_pixel)
        db.commit()
    finally:
        db.close()


def main_test():
    try:
        main.rds = FakeRedis()
        main.send_to_meta = fake_send_to_meta
        main.send_to_tiktok = fake_send_to_tiktok
        create_pixel()

        client = TestClient(main.app)

        health = client.get("/healthz")
        assert health.status_code == 200, health.text

        admin = client.get("/admin/pixels", auth=("admin", "admin"))
        assert admin.status_code == 200, admin.text
        assert "px_test" in admin.text
        assert "px_other" in admin.text
        assert "pixel-search-input" in admin.text
        assert "pixel-column-settings" in admin.text
        assert "theme-toggle" in admin.text

        tiktok_admin = client.get("/admin/tiktok/pixels", auth=("admin", "admin"))
        assert tiktok_admin.status_code == 200, tiktok_admin.text
        assert "D75QFE3C77UDH74CJM70" in tiktok_admin.text
        assert "OTHER_DATASET" in tiktok_admin.text
        assert "TT_TOKEN" not in tiktok_admin.text

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

        buyer_pixels = client.get("/admin/pixels", auth=("buyer1", "buyer-pass"))
        assert buyer_pixels.status_code == 200, buyer_pixels.text
        assert "px_test" in buyer_pixels.text
        assert "px_other" not in buyer_pixels.text
        assert "/admin/pixels/new" not in buyer_pixels.text
        assert "/admin/pixels/px_test/edit" not in buyer_pixels.text

        buyer_pixel_new = client.get("/admin/pixels/new", auth=("buyer1", "buyer-pass"))
        assert buyer_pixel_new.status_code == 403, buyer_pixel_new.text

        buyer_tiktok_pixels = client.get("/admin/tiktok/pixels", auth=("buyer1", "buyer-pass"))
        assert buyer_tiktok_pixels.status_code == 200, buyer_tiktok_pixels.text
        assert "D75QFE3C77UDH74CJM70" in buyer_tiktok_pixels.text
        assert "OTHER_DATASET" not in buyer_tiktok_pixels.text

        quality = client.get("/admin/quality", auth=("admin", "admin"))
        assert quality.status_code == 200, quality.text
        assert "Tracking Quality" in quality.text

        diagnostics_empty = client.get("/admin/diagnostics", auth=("admin", "admin"))
        assert diagnostics_empty.status_code == 200, diagnostics_empty.text
        assert "Click Diagnostics" in diagnostics_empty.text

        buyer_users = client.get("/admin/users", auth=("buyer1", "buyer-pass"))
        assert buyer_users.status_code == 403, buyer_users.text

        whale_payload = {
            "source": "whale",
            "event": "CompletePayment",
            "event_id": "conversion_1",
            "status": "Approved",
            "payout": "14.50",
            "offer_id": "offer_1",
            "flow_id": "flow-main",
            "source_id": "source_1",
            "click_uuid": "click_uuid_1",
            "ip": "127.0.0.1",
            "ttclid": "ttclid_1",
            "pixel_id": "D75QFE3C77UDH74CJM70",
            "campaign_id": "campaign_1",
            "campaign_name": "Campaign",
            "adgroup_id": "adgroup_1",
            "adgroup_name": "Adgroup",
            "creative_id": "creative_1",
            "creative_name": "Creative",
            "created_at": "2026-06-30T10:00:00Z",
            "updated_at": "2026-06-30T10:01:00Z",
        }
        whale = client.post(
            "/postbacks/whale/tiktok?secret=whale-secret",
            json=whale_payload,
        )
        assert whale.status_code == 200, whale.text
        assert whale.json()["ok"] is True
        assert whale.json()["dataset_id"] == "D75QFE3C77UDH74CJM70"
        assert len(fake_send_to_tiktok.calls) == 1
        sent_payload = fake_send_to_tiktok.calls[-1][0]
        assert sent_payload["event_source_id"] == "D75QFE3C77UDH74CJM70"
        assert sent_payload["data"][0]["event"] == "CompletePayment"
        assert sent_payload["data"][0]["event_id"] == "conversion_1"
        assert sent_payload["data"][0]["user"]["ttclid"] == "ttclid_1"
        assert sent_payload["data"][0]["properties"]["value"] == 14.5
        assert sent_payload["data"][0]["event_time"] == 1782813660

        duplicate_whale = client.post(
            "/postbacks/whale/tiktok",
            json=whale_payload,
            headers={"Authorization": "Bearer whale-secret"},
        )
        assert duplicate_whale.status_code == 200, duplicate_whale.text
        assert duplicate_whale.json()["duplicate"] is True
        assert len(fake_send_to_tiktok.calls) == 1

        pending_payload = dict(whale_payload)
        pending_payload["event_id"] = "conversion_pending"
        pending_payload["status"] = "Pending"
        pending = client.post("/postbacks/whale/tiktok?secret=whale-secret", json=pending_payload)
        assert pending.status_code == 200, pending.text
        assert pending.json()["ignored"] is True
        assert pending.json()["reason"] == "status_not_allowed"
        assert len(fake_send_to_tiktok.calls) == 1

        unknown_payload = dict(whale_payload)
        unknown_payload["event_id"] = "conversion_unknown"
        unknown_payload["pixel_id"] = "unknown_tt_pixel"
        unknown_payload["flow_id"] = "unknown_flow"
        unknown = client.post("/postbacks/whale/tiktok?secret=whale-secret", json=unknown_payload)
        assert unknown.status_code == 400, unknown.text

        tiktok_logs = client.get("/admin/tiktok/logs?buyer=__all__&event=CompletePayment", auth=("admin", "admin"))
        assert tiktok_logs.status_code == 200, tiktok_logs.text
        assert "conversion_1" in tiktok_logs.text
        assert "TT_TOKEN" not in tiktok_logs.text

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
        logged_payload = main.meta_payload_for_log({
            "data": [{"event_name": "Lead"}],
            "access_token": "TEST_ACCESS_TOKEN_123456",
        })
        assert "TEST_ACCESS_TOKEN_123456" not in logged_payload
        assert "TEST...3456" in logged_payload

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
            "sended": logged_payload,
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
        assert '<th data-col="sended">sended</th>' in lead_logs.text
        assert "TEST_ACCESS_TOKEN_123456" not in lead_logs.text
        assert "TEST...3456" in lead_logs.text
        assert "pageview_click_149" not in lead_logs.text

        print("smoke ok")
    finally:
        main.engine.dispose()
        tmpdir.cleanup()


if __name__ == "__main__":
    main_test()
