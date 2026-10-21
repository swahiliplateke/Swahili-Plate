import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.pool import StaticPool

import app as service
from app import validate_order


class ValidateOrderTests(unittest.TestCase):
    def valid_payload(self):
        return {
            "customer_name": " Amina Hassan ",
            "customer_phone": "0712345678",
            "customer_email": "amina@example.com",
            "branch": "CBD",
            "ordering_mode": "Delivery",
            "cart_items": '[{"name":"Pilau","price":650,"quantity":1}]',
            "grand_total": 650,
            "order_status": "Completed",
            "id": "client-controlled-id",
        }

    def test_assigns_server_id_and_initial_status(self):
        order = validate_order(self.valid_payload())
        self.assertNotEqual(order["id"], "client-controlled-id")
        self.assertEqual(order["customer_name"], "Amina Hassan")
        self.assertEqual(order["order_status"], "New")
        self.assertEqual(order["delivery_status"], "Waiting for rider")
        self.assertTrue(order["order_timestamp"])

    def test_rejects_unknown_branch(self):
        payload = self.valid_payload()
        payload["branch"] = "Unknown"
        with self.assertRaises(HTTPException) as error:
            validate_order(payload)
        self.assertEqual(error.exception.status_code, 422)

    def test_rejects_invalid_cart_json(self):
        payload = self.valid_payload()
        payload["cart_items"] = "not-json"
        with self.assertRaises(HTTPException) as error:
            validate_order(payload)
        self.assertEqual(error.exception.status_code, 422)


class OrderApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.client_context = TestClient(service.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        service.engine.dispose()

    def setUp(self):
        with service.engine.begin() as db:
            db.execute(delete(service.orders_table))
            db.execute(delete(service.settings_table))
            db.execute(delete(service.reviews_table))

    def test_customer_admin_and_delivery_tracking_flow(self):
        order_response = self.client.post("/api/orders", json={
            "customer_name": "Amina Hassan",
            "customer_phone": "0712 345 678",
            "customer_email": "amina@example.com",
            "branch": "CBD",
            "ordering_mode": "Delivery",
            "cart_items": [{"name": "Pilau", "price": 650, "quantity": 1}],
            "subtotal": 650,
            "grand_total": 650,
        })
        self.assertEqual(order_response.status_code, 201)
        order = order_response.json()
        self.assertEqual(self.client.get("/api/orders").status_code, 401)

        headers = {"X-Admin-Token": service.ADMIN_TOKEN}
        listing = self.client.get("/api/orders", headers=headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["id"], order["id"])

        update = self.client.patch(
            f"/api/orders/{order['id']}",
            headers=headers,
            json={"delivery_status": "On the way", "delivery_update": "Your rider is nearby."},
        )
        self.assertEqual(update.status_code, 200)

        tracking = self.client.get("/api/deliveries/0712345678")
        self.assertEqual(tracking.status_code, 200)
        self.assertEqual(tracking.json()["delivery_status"], "On the way")
        self.assertNotIn("customer_email", tracking.json())

    def test_assignment_sends_status_updates_and_shows_rider_to_customer(self):
        headers = {"X-Admin-Token": service.ADMIN_TOKEN}
        with (
            patch.object(service, "email_notifications_configured", return_value=True),
            patch.object(service, "whatsapp_notifications_configured", return_value=True),
            patch.object(service, "send_email_notification", new_callable=AsyncMock, return_value="sent") as email_sender,
            patch.object(service, "send_whatsapp_notification", new_callable=AsyncMock, return_value="sent") as whatsapp_sender,
        ):
            created = self.client.post("/api/orders", json={
                "customer_name": "Amina Hassan",
                "customer_phone": "0712 345 678",
                "customer_email": "amina@example.com",
                "whatsapp_updates_opt_in": True,
                "branch": "CBD",
                "ordering_mode": "Delivery",
                "cart_items": [{"name": "Chicken Biryani", "price": 900, "quantity": 1}],
            })
            self.assertEqual(created.status_code, 201)
            order = created.json()
            self.assertEqual(order["notification_history"][-1]["event"], "New")

            confirmed = self.client.patch(
                f"/api/orders/{order['id']}", headers=headers, json={"order_status": "Confirmed"}
            )
            self.assertEqual(confirmed.status_code, 200)

            dispatched = self.client.patch(
                f"/api/orders/{order['id']}",
                headers=headers,
                json={
                    "order_status": "Out for delivery",
                    "delivery_status": "Accepted",
                    "rider_name": "Wanjiku Njeri",
                    "rider_phone": "0711112222",
                    "delivery_update": "Your order is on the way.",
                },
            )
            self.assertEqual(dispatched.status_code, 200)
            self.assertEqual(dispatched.json()["rider_name"], "Wanjiku Njeri")
            self.assertEqual(dispatched.json()["notification_history"][-1]["event"], "Out for delivery")
            tracking = self.client.get("/api/deliveries/0712345678")
            self.assertEqual(tracking.status_code, 200)
            self.assertEqual(tracking.json()["order_status"], "Out for delivery")
            self.assertEqual(tracking.json()["rider_name"], "Wanjiku Njeri")
            self.assertEqual(tracking.json()["rider_phone"], "0711112222")
            self.assertEqual(email_sender.await_count, 3)
            self.assertEqual(whatsapp_sender.await_count, 3)

    def test_master_session_is_distinct_and_revenue_is_master_only(self):
        with patch.object(service, "MASTER_ADMIN_TOKEN", "master-test-secret"):
            regular = self.client.get("/api/admin/session", headers={"X-Admin-Token": service.ADMIN_TOKEN})
            master = self.client.get("/api/admin/session", headers={"X-Admin-Token": "master-test-secret"})
            self.assertEqual(regular.status_code, 200)
            self.assertEqual(regular.json()["role"], "admin")
            self.assertEqual(master.status_code, 200)
            self.assertEqual(master.json()["role"], "master")
            self.assertEqual(self.client.get("/api/admin/revenue", headers={"X-Admin-Token": service.ADMIN_TOKEN}).status_code, 403)
            self.assertEqual(self.client.get("/api/admin/revenue").status_code, 401)

    def test_revenue_summary_counts_six_nairobi_calendar_months_and_persists(self):
        now = datetime(2026, 10, 20, 12, tzinfo=service.BUSINESS_TIMEZONE)
        months = service.revenue_months(now)
        inside_month = f"{months[0]['key']}-02T12:00:00+03:00"
        current_month = f"{months[-1]['key']}-05T12:00:00+03:00"
        outside_month = f"{int(months[0]['key'][:4]) - 1}-12-15T12:00:00+03:00"
        rows = [
            ({"order_status": "Completed", "order_timestamp": inside_month, "grand_total": 100}, now),
            ({"order_status": "Completed", "order_timestamp": current_month, "grand_total": 350}, now),
            ({"order_status": "Cancelled", "order_timestamp": current_month, "grand_total": 900}, now),
            ({"order_status": "New", "order_timestamp": current_month, "grand_total": 700}, now),
            ({"order_status": "Completed", "order_timestamp": outside_month, "grand_total": 500}, now),
        ]
        summary = service.build_revenue_summary(rows, now)
        self.assertEqual(len(summary["months"]), 6)
        self.assertEqual(summary["total_revenue"], 450)
        self.assertEqual(summary["order_count"], 2)
        self.assertEqual(summary["months"][0]["revenue"], 100)
        self.assertEqual(summary["months"][-1]["revenue"], 350)

        placed_at = datetime.now(timezone.utc)
        completed_order = {
            "id": "revenue-test-order",
            "order_status": "Completed",
            "order_timestamp": placed_at.isoformat(),
            "grand_total": 1800,
        }
        with service.engine.begin() as db:
            db.execute(insert(service.orders_table).values(id=completed_order["id"], payload=completed_order, created_at=placed_at, updated_at=placed_at))
        with patch.object(service, "MASTER_ADMIN_TOKEN", "master-test-secret"):
            response = self.client.get("/api/admin/revenue", headers={"X-Admin-Token": "master-test-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["total_revenue"], 1800)
        with service.engine.connect() as db:
            snapshot = db.execute(
                select(service.settings_table.c.payload).where(service.settings_table.c.setting_key == service.REVENUE_SETTING_KEY)
            ).scalar_one()
        self.assertEqual(snapshot, response.json())

    def test_dispatch_requires_both_rider_details(self):
        created = self.client.post("/api/orders", json={
            "customer_name": "Demo Guest",
            "customer_phone": "0791234567",
            "branch": "CBD",
            "ordering_mode": "Delivery",
            "cart_items": [],
        })
        response = self.client.patch(
            f"/api/orders/{created.json()['id']}",
            headers={"X-Admin-Token": service.ADMIN_TOKEN},
            json={"order_status": "Out for delivery"},
        )
        self.assertEqual(response.status_code, 422)

    def test_legacy_menu_paths_normalize_to_deployed_web_images(self):
        legacy = "Assets(%20Swahili%20plate)/menu-product-images/chicken-biryani.jpg"
        saved = self.client.put(
            "/api/settings/menu",
            headers={"X-Admin-Token": service.ADMIN_TOKEN},
            json={"food": [{"name": "Chicken Biryani", "category": "Mains", "price": 900, "image": legacy}], "drinks": []},
        )
        self.assertEqual(saved.status_code, 200)
        image_url = saved.json()["value"]["food"][0]["image"]
        self.assertEqual(image_url, "/brand-assets/web-ready/menu-product-images/chicken-biryani.jpg")
        self.assertEqual(self.client.get(image_url).status_code, 200)

    def test_customer_product_photo_index_returns_served_optimized_assets(self):
        response = self.client.get("/api/customer-product-images")
        self.assertEqual(response.status_code, 200)
        images = response.json()["images"]
        self.assertGreaterEqual(len(images), 100)
        self.assertFalse(any("image review for fixing" in image["name"].casefold() for image in images))
        chicken = next(image for image in images if image["name"] == "chicken-biryani")
        self.assertTrue(chicken["image"].startswith("/brand-assets/web-ready/"))
        for image in images:
            self.assertEqual(self.client.get(image["image"]).status_code, 200, image["image"])

    def test_admin_image_library_and_notification_setup_status(self):
        headers = {"X-Admin-Token": service.ADMIN_TOKEN}
        library = self.client.get("/api/menu-images", headers=headers)
        self.assertEqual(library.status_code, 200)
        self.assertTrue(library.json()["images"])
        self.assertTrue(all(item["image"].startswith("/brand-assets/web-ready/") for item in library.json()["images"]))
        status = self.client.get("/api/notifications/status", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertIn("email_configured", status.json())
        self.assertIn("whatsapp_configured", status.json())

    def test_legacy_branch_photos_normalize_to_deployed_web_images(self):
        headers = {"X-Admin-Token": service.ADMIN_TOKEN}
        branches = {
            "business": {"name": "Swahili Plate", "description": "Coastal meals", "phone": "0711726448", "email": "hello@example.com"},
            "branches": {
                "CBD": {"address": "CBD", "phone": "0711726448", "images": ["Assets(%20Swahili%20plate)/Swahili%20plate.%20assets/take%203.jpg"]}
            },
        }
        saved = self.client.put("/api/settings/branches", headers=headers, json=branches)
        self.assertEqual(saved.status_code, 200)
        image_url = saved.json()["value"]["branches"]["CBD"]["images"][0]
        self.assertEqual(image_url, "/brand-assets/web-ready/Swahili%20plate.%20assets/take%203.jpg")
        self.assertEqual(self.client.get(image_url).status_code, 200)

    def test_reservation_payload_is_accepted(self):
        response = self.client.post("/api/orders", json={
            "customer_name": "Demo Guest",
            "customer_phone": "0712345678",
            "customer_email": "guest@example.com",
            "branch": "CBD",
            "ordering_mode": "Dine in",
            "cart_items": "Table reservation",
            "reservation_date": "2026-09-26",
            "reservation_time": "19:00",
            "party_size": 2,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["cart_items"], "Table reservation")

    def test_admin_can_update_status_for_non_delivery_order(self):
        response = self.client.post("/api/orders", json={
            "customer_name": "Local Website Test",
            "customer_phone": "0790000123",
            "customer_email": "local-test@example.com",
            "branch": "CBD",
            "ordering_mode": "Take out",
            "cart_items": [{"name": "Pilau", "price": 650, "quantity": 1}],
            "grand_total": 650,
        })
        order = response.json()

        updated = self.client.patch(
            f"/api/orders/{order['id']}",
            headers={"X-Admin-Token": service.ADMIN_TOKEN},
            json={"order_status": "Cancelled", "delivery_status": ""},
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["order_status"], "Cancelled")

    def test_shared_settings_are_publicly_readable_and_admin_writable(self):
        missing = self.client.get("/api/settings/menu")
        self.assertEqual(missing.status_code, 200)
        self.assertIsNone(missing.json()["value"])

        menu = {"food": [{"name": "Pilau", "category": "Mains", "price": 650}], "drinks": []}
        self.assertEqual(self.client.put("/api/settings/menu", json=menu).status_code, 401)
        saved = self.client.put("/api/settings/menu", headers={"X-Admin-Token": service.ADMIN_TOKEN}, json=menu)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get("/api/settings/menu").json()["value"], menu)

    def test_customer_review_is_persisted_for_admin(self):
        review = {"name": "Demo Guest", "branch": "CBD", "rating": 5, "message": "Warm service and delicious food."}
        submitted = self.client.post("/api/reviews", json=review)
        self.assertEqual(submitted.status_code, 201)
        self.assertEqual(self.client.get("/api/reviews").status_code, 401)
        listing = self.client.get("/api/reviews", headers={"X-Admin-Token": service.ADMIN_TOKEN})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["message"], review["message"])

    def test_project_files_are_not_publicly_served(self):
        redirect = self.client.get("/", follow_redirects=False)
        self.assertEqual(redirect.status_code, 307)
        self.assertEqual(redirect.headers["location"], "/customer/")
        self.assertEqual(self.client.get("/customer/").status_code, 200)

    def test_customer_menu_catalog_uses_source_taxonomy_and_image_audit(self):
        html = Path("Customer Site.html").read_text(encoding="utf-8")
        for name in [
            "Spanish Omelette",
            "Rolex",
            "Chicken Biryani",
            "Fish Platter",
            "Mutton Mandhi",
            "Wraps / Shawarma",
            "Breakfast Combos",
            "Fish / Seafood",
            "MENU_IMAGE_AUDIT",
            "PRODUCT_IMAGE_MAP",
        ]:
            self.assertIn(name, html)

    def test_customer_and_admin_pwa_assets_are_scoped(self):
        customer_manifest = self.client.get("/customer/manifest.webmanifest")
        self.assertEqual(customer_manifest.status_code, 200)
        self.assertEqual(self.client.get("/customer/service-worker.js").status_code, 200)
        admin_manifest = self.client.get("/admin/manifest.webmanifest")
        self.assertEqual(admin_manifest.status_code, 200)
        self.assertEqual(self.client.get("/admin/service-worker.js").status_code, 200)
        customer_scope = customer_manifest.json()["scope"]
        admin_scope = admin_manifest.json()["scope"]
        self.assertEqual(customer_scope, "/customer/")
        self.assertEqual(admin_scope, "/admin/")
        self.assertFalse(customer_scope.startswith(admin_scope))
        self.assertFalse(admin_scope.startswith(customer_scope))
        for manifest in (customer_manifest.json(), admin_manifest.json()):
            for icon in manifest["icons"]:
                self.assertEqual(self.client.get(icon["src"]).status_code, 200)
        redirect = self.client.get("/admin", follow_redirects=False)
        self.assertEqual(redirect.status_code, 307)
        self.assertEqual(redirect.headers["location"], "/admin/")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/brand-assets/20260925_213031_0000.svg").status_code, 200)
        self.assertEqual(self.client.get("/.env.example").status_code, 404)
        self.assertEqual(self.client.get("/app.py").status_code, 404)


if __name__ == "__main__":
    unittest.main()
