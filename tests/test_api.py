import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
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
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/brand-assets/20260925_213031_0000.svg").status_code, 200)
        self.assertEqual(self.client.get("/.env.example").status_code, 404)
        self.assertEqual(self.client.get("/app.py").status_code, 404)


if __name__ == "__main__":
    unittest.main()