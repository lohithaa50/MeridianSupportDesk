import base64
import io
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

import home


class SupportDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = home.DB_PATH
        home.DB_PATH = Path(self.temp_dir.name) / "test.db"

        connection = sqlite3.connect(home.DB_PATH)
        connection.executescript("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE pharmacies (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                city TEXT NOT NULL
            );

            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                customer_id INTEGER NOT NULL,
                pharmacy_id INTEGER NOT NULL,
                delivery_status TEXT NOT NULL
            );

            CREATE TABLE staff_users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT
            );

            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                issue_type TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                assigned_staff_id INTEGER,
                photo_path TEXT
            );

            CREATE TABLE ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                sender_type TEXT NOT NULL,
                staff_user_id INTEGER,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                changed_by_staff_id INTEGER,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE reply_templates (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                body TEXT NOT NULL
            );

            INSERT INTO customers VALUES (1, 'Avery Johnson');
            INSERT INTO pharmacies VALUES
                (1, 'Meridian Midtown Pharmacy', 'New York');
            INSERT INTO orders VALUES
                (1, 'MD-20481', 1, 1, 'In transit');
            INSERT INTO staff_users
                (id, name, email, role, password_hash)
                VALUES (1, 'Morgan Lee', 'morgan@example.com',
                        'support_agent', 'test-hash');

            INSERT INTO tickets
                (id, customer_id, order_id, issue_type, description, status)
                VALUES (1, 1, 1, 'late_delivery', 'Package is late', 'open');

            INSERT INTO ticket_messages
                (ticket_id, sender_type, message)
                VALUES (1, 'customer', 'Package is late');

            INSERT INTO status_history
                (ticket_id, old_status, new_status)
                VALUES (1, NULL, 'open');

            INSERT INTO reply_templates
                (id, name, issue_type, body)
                VALUES (1, 'Late delivery update', 'late_delivery',
                        'Hi {customer_name}, order {order_number} is being checked.');
        """)
        connection.close()

        home.app.config["TESTING"] = True
        self.client = home.app.test_client()

    def tearDown(self):
        home.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_customer_can_submit_a_ticket(self):
        response = self.client.post(
            "/submit",
            data={
                "customer_id": "1",
                "order_id": "1",
                "issue_type": "missing_item",
                "description": "An item is missing"
            }
        )

        self.assertEqual(response.status_code, 200)

        connection = sqlite3.connect(home.DB_PATH)
        ticket = connection.execute(
            "SELECT id, status FROM tickets ORDER BY id DESC LIMIT 1"
        ).fetchone()
        message = connection.execute(
            """
            SELECT message FROM ticket_messages
            WHERE ticket_id = ?
            """,
            (ticket[0],)
        ).fetchone()
        connection.close()

        self.assertEqual(ticket[1], "open")
        self.assertEqual(message[0], "An item is missing")

    def test_staff_ticket_list_loads(self):
        response = self.client.get("/staff/tickets")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Support tickets", response.data)
        self.assertIn(b"MD-20481", response.data)

    def test_ticket_detail_loads(self):
        response = self.client.get("/staff/tickets/1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MD-20481", response.data)
        self.assertIn(b"Avery Johnson", response.data)
        self.assertIn(b"Late delivery update", response.data)

    def test_allowed_status_is_saved_and_recorded(self):
        response = self.client.post(
            "/staff/tickets/1/status",
            data={"status": "in progress"}
        )

        self.assertEqual(response.status_code, 302)

        connection = sqlite3.connect(home.DB_PATH)
        status = connection.execute(
            "SELECT status FROM tickets WHERE id = 1"
        ).fetchone()[0]
        history = connection.execute(
            """
            SELECT old_status, new_status
            FROM status_history
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        connection.close()

        self.assertEqual(status, "in progress")
        self.assertEqual(history, ("open", "in progress"))

    def test_invalid_status_is_rejected(self):
        response = self.client.post(
            "/staff/tickets/1/status",
            data={"status": "cancelled"}
        )

        self.assertEqual(response.status_code, 400)

        connection = sqlite3.connect(home.DB_PATH)
        status = connection.execute(
            "SELECT status FROM tickets WHERE id = 1"
        ).fetchone()[0]
        connection.close()

        self.assertEqual(status, "open")

    def test_missing_ticket_returns_404(self):
        response = self.client.post(
            "/staff/tickets/999/status",
            data={"status": "resolved"}
        )

        self.assertEqual(response.status_code, 404)

    def test_staff_reply_is_saved(self):
        response = self.client.post(
            "/staff/tickets/1/reply",
            data={"message": "We are checking your order."}
        )

        self.assertEqual(response.status_code, 302)

        connection = sqlite3.connect(home.DB_PATH)
        message = connection.execute(
            """
            SELECT sender_type, staff_user_id, message
            FROM ticket_messages
            WHERE ticket_id = 1
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        connection.close()

        self.assertEqual(
            message,
            ("staff", 1, "We are checking your order.")
        )

    def test_photo_input_belongs_to_support_request_form(self):
        class FormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms = []
                self.current = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "form":
                    self.current = {"attrs": attrs, "fields": set()}
                    self.forms.append(self.current)
                if self.current is not None and attrs.get("name"):
                    self.current["fields"].add(attrs["name"])

            def handle_endtag(self, tag):
                if tag == "form":
                    self.current = None

        parser = FormParser()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        parser.feed(response.get_data(as_text=True))
        forms = [form for form in parser.forms if form["attrs"].get("action") == "/submit"]
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["attrs"].get("enctype"), "multipart/form-data")
        self.assertTrue({"customer_id", "order_id", "issue_type", "description", "damage_photo"} <= forms[0]["fields"])

    def test_uploaded_photo_is_visible_to_customer_and_staff(self):
        original_uploads = home.UPLOAD_FOLDER
        original_static = home.app.static_folder
        home.UPLOAD_FOLDER = Path(self.temp_dir.name) / "static" / "uploads"
        home.app.static_folder = str(home.UPLOAD_FOLDER.parent)
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNKsAAAAASUVORK5CYII="
        )
        try:
            response = self.client.post("/submit", data={
                "customer_id": "1", "order_id": "1", "issue_type": "damaged_item",
                "description": "Damaged package",
                "damage_photo": (io.BytesIO(image), "damage.png"),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 200)
            connection = sqlite3.connect(home.DB_PATH)
            try:
                ticket_id, photo_path = connection.execute(
                    "SELECT id, photo_path FROM tickets ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(photo_path)
            image_url = f"/static/{photo_path}"
            for page in ("/my-tickets", f"/staff/tickets/{ticket_id}"):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                self.assertIn(image_url.encode(), response.data)
            response = self.client.get(image_url)
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, image)
            finally:
                response.close()
        finally:
            home.UPLOAD_FOLDER = original_uploads
            home.app.static_folder = original_static

    def test_customer_can_resolve_active_ticket_and_record_message(self):
        with self.client.session_transaction() as session:
            session["customer_id"] = 1
        response = self.client.get("/my-tickets")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mark as resolved", response.data)
        response = self.client.post("/my-tickets/1/resolve", data={
            "message": "The pharmacy replaced the item.",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Resolved", response.data)
        self.assertIn(b"Ticket marked as resolved", response.data)
        self.assertNotIn(b"Mark as resolved", response.data)
        connection = sqlite3.connect(home.DB_PATH)
        try:
            self.assertEqual(connection.execute("SELECT status FROM tickets WHERE id = 1").fetchone()[0], "resolved")
            self.assertEqual(connection.execute(
                "SELECT sender_type, staff_user_id, message FROM ticket_messages ORDER BY id DESC LIMIT 1"
            ).fetchone(), ("customer", None, "The pharmacy replaced the item."))
            self.assertEqual(connection.execute(
                "SELECT old_status, new_status, changed_by_staff_id FROM status_history ORDER BY id DESC LIMIT 1"
            ).fetchone(), ("open", "resolved", None))
        finally:
            connection.close()
        self.client.post("/my-tickets/1/resolve", data={"message": "Duplicate click"})
        connection = sqlite3.connect(home.DB_PATH)
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM status_history WHERE ticket_id = 1 AND new_status = 'resolved'"
            ).fetchone()[0], 1)
        finally:
            connection.close()

    def test_customer_resolve_checks_session_owner_and_allows_empty_message(self):
        response = self.client.post("/my-tickets/1/resolve", data={"message": "Fixed"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        with self.client.session_transaction() as session:
            session["customer_id"] = 2
        self.assertEqual(self.client.post(
            "/my-tickets/1/resolve", data={"message": "Not my ticket"}
        ).status_code, 404)
        with self.client.session_transaction() as session:
            session["customer_id"] = 1
        response = self.client.post("/my-tickets/1/resolve", data={"message": "  "}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ticket marked as resolved", response.data)
        self.assertEqual(self.client.post(
            "/my-tickets/999/resolve", data={"message": "Fixed"}
        ).status_code, 404)
        connection = sqlite3.connect(home.DB_PATH)
        try:
            self.assertEqual(connection.execute("SELECT status FROM tickets WHERE id = 1").fetchone()[0], "resolved")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM status_history").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_messages").fetchone()[0], 1)
        finally:
            connection.close()

    def test_customer_cannot_resolve_closed_ticket(self):
        connection = sqlite3.connect(home.DB_PATH)
        try:
            connection.execute("UPDATE tickets SET status = 'closed' WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
        with self.client.session_transaction() as session:
            session["customer_id"] = 1
        self.assertEqual(self.client.post(
            "/my-tickets/1/resolve", data={"message": "Fixed"}
        ).status_code, 400)
        response = self.client.get("/my-tickets")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Mark as resolved", response.data)

    def test_customer_can_attach_photo_to_existing_ticket(self):
        original_uploads = home.UPLOAD_FOLDER
        home.UPLOAD_FOLDER = Path(self.temp_dir.name) / "uploads"
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNKsAAAAASUVORK5CYII="
        )
        try:
            with self.client.session_transaction() as session:
                session["customer_id"] = 1
            response = self.client.get("/my-tickets")
            self.assertIn(b'action="/my-tickets/1/photo"', response.data)
            response = self.client.post("/my-tickets/1/photo", data={
                "damage_photo": (io.BytesIO(image), "package.png"),
            }, content_type="multipart/form-data", follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Photo attached to your ticket", response.data)
            self.assertIn(b"View photo", response.data)
            self.assertNotIn(b'action="/my-tickets/1/photo"', response.data)
            connection = sqlite3.connect(home.DB_PATH)
            try:
                status, path = connection.execute(
                    "SELECT status, photo_path FROM tickets WHERE id = 1"
                ).fetchone()
                self.assertEqual(status, "open")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
            finally:
                connection.close()
            self.assertEqual((home.UPLOAD_FOLDER / Path(path).name).read_bytes(), image)
            self.assertIn(f"/static/{path}".encode(), self.client.get("/staff/tickets/1").data)
            response = self.client.post("/my-tickets/1/photo", data={
                "damage_photo": (io.BytesIO(image), "another.png"),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(len(list(home.UPLOAD_FOLDER.iterdir())), 1)
        finally:
            home.UPLOAD_FOLDER = original_uploads

    def test_existing_ticket_upload_checks_owner_and_file(self):
        original_uploads = home.UPLOAD_FOLDER
        home.UPLOAD_FOLDER = Path(self.temp_dir.name) / "uploads"
        try:
            response = self.client.post("/my-tickets/1/photo")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")
            with self.client.session_transaction() as session:
                session["customer_id"] = 2
            response = self.client.post("/my-tickets/1/photo", data={
                "damage_photo": (io.BytesIO(b"test"), "photo.png"),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 404)
            with self.client.session_transaction() as session:
                session["customer_id"] = 1
            response = self.client.post("/my-tickets/1/photo", follow_redirects=True)
            self.assertIn(b"Choose a photo to attach", response.data)
            response = self.client.post("/my-tickets/1/photo", data={
                "damage_photo": (io.BytesIO(b"test"), "photo.txt"),
            }, content_type="multipart/form-data", follow_redirects=True)
            self.assertIn(b"Upload a PNG, JPG, JPEG, or WEBP image", response.data)
            connection = sqlite3.connect(home.DB_PATH)
            try:
                self.assertIsNone(connection.execute("SELECT photo_path FROM tickets WHERE id = 1").fetchone()[0])
            finally:
                connection.close()
            self.assertFalse(home.UPLOAD_FOLDER.exists())
        finally:
            home.UPLOAD_FOLDER = original_uploads

    def test_optional_photo_on_missing_item_request_is_saved(self):
        original_uploads = home.UPLOAD_FOLDER
        home.UPLOAD_FOLDER = Path(self.temp_dir.name) / "uploads"
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aNKsAAAAASUVORK5CYII="
        )
        try:
            response = self.client.post("/submit", data={
                "customer_id": "1", "order_id": "1", "issue_type": "missing_item",
                "description": "An item is missing", "damage_photo": (io.BytesIO(image), "package.png"),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 200)
            connection = sqlite3.connect(home.DB_PATH)
            try:
                path = connection.execute("SELECT photo_path FROM tickets ORDER BY id DESC LIMIT 1").fetchone()[0]
            finally:
                connection.close()
            self.assertIsNotNone(path)
            self.assertEqual((home.UPLOAD_FOLDER / Path(path).name).read_bytes(), image)
            self.assertIn(f"/static/{path}".encode(), self.client.get("/my-tickets").data)
        finally:
            home.UPLOAD_FOLDER = original_uploads

    def test_ticket_can_be_assigned(self):
        response = self.client.post(
            "/staff/tickets/1/assign",
            data={"staff_user_id": "1"}
        )

        self.assertEqual(response.status_code, 302)

        connection = sqlite3.connect(home.DB_PATH)
        assigned_staff_id = connection.execute(
            "SELECT assigned_staff_id FROM tickets WHERE id = 1"
        ).fetchone()[0]
        connection.close()

        self.assertEqual(assigned_staff_id, 1)


if __name__ == "__main__":
    unittest.main()
