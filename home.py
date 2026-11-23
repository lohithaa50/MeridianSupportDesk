import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
import smtplib
from email.message import EmailMessage

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename


DB_PATH = Path(__file__).with_name("meridian.db")
UPLOAD_FOLDER = Path(__file__).with_name("static") / "uploads"
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

RESPONSE_TARGET_HOURS = {
    "missing_item": 2,
    "late_delivery": 4,
    "damaged_item": 4,
    "wrong_item": 4,
    "billing_question": 24,
    "other": 24,
}

STAFF_STATUSES = (
    "open",
    "in progress",
    "waiting on customer",
    "resolved",
)

app = Flask(__name__)
app.secret_key = "meridian-local-demo-key"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # Maximum image size: 5 MB

def send_reply_notification(ticket_id, customer_name, order_number, message):
    email = EmailMessage()
    email["Subject"] = f"Update on support ticket #{ticket_id}"
    email["From"] = "support@meridian.example.test"
    email["To"] = "customer@example.test"
    email.set_content(
        f"Hello {customer_name},\n\n"
        f"Support replied about order {order_number} "
        f"(ticket #{ticket_id}):\n\n"
        f"{message}\n\n"
        "Meridian Support"
    )

    try:
        with smtplib.SMTP("localhost", 1025, timeout=5) as mail_server:
            mail_server.send_message(email)
        print(f"Test email for ticket #{ticket_id} captured by MailHog.")
    except (OSError, smtplib.SMTPException) as error:
        # Keep the staff reply saved even if MailHog is not running.
        print(f"Could not connect to MailHog: {error}")
def connect_db():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def parse_timestamp(value):
    """Parse SQLite or ISO timestamps and return a UTC-aware datetime."""
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)

    return timestamp.astimezone(timezone.utc)


@app.get("/")
def home():
    connection = connect_db()
    customers = connection.execute(
        "SELECT id, name FROM customers ORDER BY name"
    ).fetchall()
    orders = connection.execute("""
        SELECT id, order_number, customer_id, delivery_status
        FROM orders
        ORDER BY order_number
    """).fetchall()
    connection.close()

    return render_template(
        "index.html",
        customers=customers,
        orders=orders,
    )


@app.post("/submit")
def submit():
    customer_id = request.form.get("customer_id", type=int)
    order_id = request.form.get("order_id", type=int)
    issue = request.form.get("issue_type", "")
    description = request.form.get("description", "").strip()
    photo = request.files.get("damage_photo")

    if issue not in RESPONSE_TARGET_HOURS or not description:
        flash("Choose an issue type and enter a description.", "error")
        return redirect(url_for("home"))

    # A damaged-item report must include an image.
    if issue == "damaged_item" and (not photo or not photo.filename):
        flash("Please attach a photo of the damaged item.", "error")
        return redirect(url_for("home"))

    # Check the image extension before saving it.
    photo_path = None
    safe_name = None
    extension = None

    if photo and photo.filename:
        safe_name = secure_filename(photo.filename)
        extension = Path(safe_name).suffix.lower()

        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            flash("Upload a PNG, JPG, JPEG, or WEBP image.", "error")
            return redirect(url_for("home"))

    connection = connect_db()

    # Verify this order belongs to the selected customer.
    order = connection.execute(
        "SELECT id FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, customer_id),
    ).fetchone()

    if not customer_id or not order:
        connection.close()
        flash("Please select one of your orders.", "error")
        return redirect(url_for("home"))

    # Save the image under a generated filename.
    if photo and photo.filename:
        UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        saved_name = f"{uuid.uuid4().hex}{extension}"
        photo.save(UPLOAD_FOLDER / saved_name)
        photo_path = f"uploads/{saved_name}"

    cursor = connection.execute("""
        INSERT INTO tickets
            (customer_id, order_id, issue_type, description, photo_path)
        VALUES (?, ?, ?, ?, ?)
    """, (customer_id, order_id, issue, description, photo_path))

    ticket_id = cursor.lastrowid

    connection.execute("""
        INSERT INTO ticket_messages
            (ticket_id, sender_type, message)
        VALUES (?, 'customer', ?)
    """, (ticket_id, description))

    connection.execute("""
        INSERT INTO status_history
            (ticket_id, old_status, new_status)
        VALUES (?, NULL, 'open')
    """, (ticket_id,))

    connection.commit()
    connection.close()

    # Remember the selected demo customer for the "My tickets" page.
    session["customer_id"] = customer_id

    return render_template(
        "confirmation.html",
        ticket_id=ticket_id,
        issue_type=issue,
        response_hours=RESPONSE_TARGET_HOURS[issue],
    )


@app.get("/my-tickets")
def my_tickets():
    customer_id = session.get("customer_id")

    if not customer_id:
        flash("Submit a support request first to view your tickets.", "error")
        return redirect(url_for("home"))

    connection = connect_db()

    customer = connection.execute(
        "SELECT name FROM customers WHERE id = ?",
        (customer_id,),
    ).fetchone()

    if customer is None:
        connection.close()
        session.pop("customer_id", None)
        return redirect(url_for("home"))

    tickets = connection.execute("""
        SELECT t.id, t.issue_type, t.status, t.created_at,
               o.order_number, p.name, t.photo_path
        FROM tickets t
        JOIN orders o ON o.id = t.order_id
        JOIN pharmacies p ON p.id = o.pharmacy_id
        WHERE t.customer_id = ?
        ORDER BY t.created_at DESC
    """, (customer_id,)).fetchall()

    connection.close()

    return render_template(
        "my_tickets.html",
        customer_name=customer[0],
        tickets=tickets,
    )


@app.post("/my-tickets/<int:ticket_id>/photo")
def customer_attach_photo(ticket_id):
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect(url_for("home"))

    photo = request.files.get("damage_photo")
    if not photo or not photo.filename:
        flash("Choose a photo to attach.", "error")
        return redirect(url_for("my_tickets"))

    extension = Path(secure_filename(photo.filename)).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        flash("Upload a PNG, JPG, JPEG, or WEBP image.", "error")
        return redirect(url_for("my_tickets"))

    connection = connect_db()
    saved_file = None
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            ticket = connection.execute(
                "SELECT photo_path FROM tickets WHERE id = ? AND customer_id = ?",
                (ticket_id, customer_id),
            ).fetchone()
            if ticket is None:
                return "Ticket not found", 404
            if ticket[0]:
                return "This ticket already has a photo attached.", 409

            UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
            saved_name = f"{uuid.uuid4().hex}{extension}"
            saved_file = UPLOAD_FOLDER / saved_name
            photo.save(saved_file)
            connection.execute(
                "UPDATE tickets SET photo_path = ? WHERE id = ?",
                (f"uploads/{saved_name}", ticket_id),
            )
    except Exception:
        if saved_file is not None:
            saved_file.unlink(missing_ok=True)
        raise
    finally:
        connection.close()

    flash("Photo attached to your ticket.", "success")
    return redirect(url_for("my_tickets"))


@app.post("/my-tickets/<int:ticket_id>/resolve")
def customer_resolve_ticket(ticket_id):
    customer_id = session.get("customer_id")
    message = request.form.get("message", "").strip()

    if not customer_id:
        return redirect(url_for("home"))

    connection = connect_db()
    try:
        with connection:
            # Keep the status change and its history in one transaction.
            connection.execute("BEGIN IMMEDIATE")
            ticket = connection.execute(
                "SELECT status FROM tickets WHERE id = ? AND customer_id = ?",
                (ticket_id, customer_id),
            ).fetchone()

            if ticket is None:
                return "Ticket not found", 404

            if ticket[0] == "closed":
                return "Closed tickets cannot be marked as resolved.", 400

            if ticket[0] != "resolved":
                if ticket[0] not in STAFF_STATUSES:
                    return "This ticket cannot be marked as resolved.", 400

                connection.execute(
                    "UPDATE tickets SET status = 'resolved' WHERE id = ?",
                    (ticket_id,),
                )
                if message:
                    connection.execute("""
                        INSERT INTO ticket_messages
                            (ticket_id, sender_type, staff_user_id, message)
                        VALUES (?, 'customer', NULL, ?)
                    """, (ticket_id, message))
                connection.execute("""
                    INSERT INTO status_history
                        (ticket_id, old_status, new_status, changed_by_staff_id)
                    VALUES (?, ?, 'resolved', NULL)
                """, (ticket_id, ticket[0]))
    finally:
        connection.close()

    flash("Ticket marked as resolved. Thank you for the update.", "success")
    return redirect(url_for("my_tickets"))


@app.post("/my-tickets/<int:ticket_id>/close")
def customer_close_ticket(ticket_id):
    customer_id = session.get("customer_id")
    message = request.form.get("message", "").strip()

    if not customer_id:
        return redirect(url_for("home"))

    if not message:
        flash("Please add a short confirmation message.", "error")
        return redirect(url_for("my_tickets"))

    connection = connect_db()
    ticket = connection.execute("""
        SELECT status FROM tickets
        WHERE id = ? AND customer_id = ?
    """, (ticket_id, customer_id)).fetchone()

    if ticket is None:
        connection.close()
        return "Ticket not found", 404

    if ticket[0] != "resolved":
        connection.close()
        return "Only resolved tickets can be closed by the customer.", 400

    connection.execute(
        "UPDATE tickets SET status = 'closed' WHERE id = ?",
        (ticket_id,),
    )

    connection.execute("""
        INSERT INTO ticket_messages
            (ticket_id, sender_type, staff_user_id, message)
        VALUES (?, 'customer', NULL, ?)
    """, (ticket_id, message))

    connection.execute("""
        INSERT INTO status_history
            (ticket_id, old_status, new_status, changed_by_staff_id)
        VALUES (?, 'resolved', 'closed', NULL)
    """, (ticket_id,))

    connection.commit()
    connection.close()

    flash("Ticket closed. Thank you for confirming the issue is fixed.", "success")
    return redirect(url_for("my_tickets"))


@app.get("/api/orders")
def customer_orders():
    customer_id = request.args.get("customer_id", type=int)

    if not customer_id:
        return jsonify([])

    connection = connect_db()
    orders = connection.execute("""
        SELECT id, order_number, delivery_status
        FROM orders
        WHERE customer_id = ?
        ORDER BY order_number
    """, (customer_id,)).fetchall()
    connection.close()

    return jsonify([
        {
            "id": order[0],
            "order_number": order[1],
            "delivery_status": order[2],
        }
        for order in orders
    ])


@app.get("/staff/tickets")
def staff_tickets():
    status_filter = request.args.get("status", "")
    issue_filter = request.args.get("issue_type", "")
    pharmacy_filter = request.args.get("pharmacy_id", "")
    age_filter = request.args.get("age", "")

    conditions = []
    values = []

    if status_filter:
        conditions.append("t.status = ?")
        values.append(status_filter)

    if issue_filter:
        conditions.append("t.issue_type = ?")
        values.append(issue_filter)

    if pharmacy_filter:
        conditions.append("p.id = ?")
        values.append(pharmacy_filter)

    if age_filter == "under_24_hours":
        conditions.append("t.created_at >= datetime('now', '-24 hours')")
    elif age_filter == "over_24_hours":
        conditions.append("t.created_at < datetime('now', '-24 hours')")

    query = """
        SELECT t.id, c.name, o.order_number, p.name,
               t.issue_type, t.status, t.created_at, p.id,
               (
                   SELECT MIN(m.created_at)
                   FROM ticket_messages m
                   WHERE m.ticket_id = t.id
                     AND m.sender_type = 'staff'
               ) AS first_staff_reply_at
        FROM tickets t
        JOIN customers c ON t.customer_id = c.id
        JOIN orders o ON t.order_id = o.id
        JOIN pharmacies p ON o.pharmacy_id = p.id
    """

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY t.id DESC"

    connection = connect_db()
    tickets = connection.execute(query, values).fetchall()
    pharmacies = connection.execute(
        "SELECT id, name FROM pharmacies ORDER BY name"
    ).fetchall()
    connection.close()

    now = datetime.now(timezone.utc)
    sla_by_ticket = {}

    for ticket in tickets:
        target_hours = RESPONSE_TARGET_HOURS.get(ticket[4], 24)
        created_at = parse_timestamp(ticket[6])
        first_reply_at = ticket[8]
        response_time = parse_timestamp(first_reply_at) if first_reply_at else now

        elapsed_hours = max(
            0,
            (response_time - created_at).total_seconds() / 3600,
        )

        if first_reply_at:
            if elapsed_hours <= target_hours:
                label, color = "Responded on time", "green"
            else:
                label, color = "Responded late", "red"
        elif elapsed_hours >= target_hours:
            label, color = "Overdue", "red"
        elif elapsed_hours >= target_hours * 0.75:
            label, color = "Due soon", "orange"
        else:
            label, color = "On track", "green"

        sla_by_ticket[ticket[0]] = {
            "target_hours": target_hours,
            "label": label,
            "color": color,
        }

    issue_types = {
        "late_delivery": "Late Delivery",
        "missing_item": "Missing Item",
        "damaged_item": "Damaged Item",
        "wrong_item": "Wrong Item",
        "billing_question": "Billing Question",
        "other": "Other",
    }

    return render_template(
        "tickets.html",
        tickets=tickets,
        pharmacies=pharmacies,
        selected_status=status_filter,
        selected_issue=issue_filter,
        selected_pharmacy=pharmacy_filter,
        selected_age=age_filter,
        sla_by_ticket=sla_by_ticket,
        issue_types=issue_types,
        statuses=STAFF_STATUSES,
    )


@app.get("/staff/tickets/<int:ticket_id>")
def ticket_detail(ticket_id):
    connection = connect_db()

    ticket = connection.execute("""
        SELECT t.id, c.name, o.order_number, p.name,
               t.issue_type, t.description, t.status,
               t.created_at, o.delivery_status, t.photo_path
        FROM tickets t
        JOIN customers c ON t.customer_id = c.id
        JOIN orders o ON t.order_id = o.id
        JOIN pharmacies p ON o.pharmacy_id = p.id
        WHERE t.id = ?
    """, (ticket_id,)).fetchone()

    if ticket is None:
        connection.close()
        return "Ticket not found", 404

    messages = connection.execute("""
        SELECT m.sender_type, s.name, m.message, m.created_at
        FROM ticket_messages m
        LEFT JOIN staff_users s ON m.staff_user_id = s.id
        WHERE m.ticket_id = ?
        ORDER BY m.id
    """, (ticket_id,)).fetchall()

    staff_users = connection.execute(
        "SELECT id, name FROM staff_users ORDER BY name"
    ).fetchall()

    reply_templates = connection.execute("""
        SELECT id, name, issue_type, body
        FROM reply_templates
        ORDER BY name
    """).fetchall()

    assigned_row = connection.execute(
        "SELECT assigned_staff_id FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    assigned_staff_id = assigned_row[0] if assigned_row else None

    assigned_staff = None
    if assigned_staff_id:
        assigned_staff = connection.execute(
            "SELECT name FROM staff_users WHERE id = ?",
            (assigned_staff_id,),
        ).fetchone()

    connection.close()

    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        messages=messages,
        staff_users=staff_users,
        assigned_staff_id=assigned_staff_id,
        assigned_staff_name=assigned_staff[0] if assigned_staff else None,
        reply_templates=reply_templates,
    )


@app.post("/staff/tickets/<int:ticket_id>/status")
def update_ticket_status(ticket_id):
    status = request.form.get("status")

    if status not in STAFF_STATUSES:
        return "Invalid status", 400

    connection = connect_db()
    current = connection.execute(
        "SELECT status FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()

    if current is None:
        connection.close()
        return "Ticket not found", 404

    old_status = current[0]

    if old_status != status:
        connection.execute(
            "UPDATE tickets SET status = ? WHERE id = ?",
            (status, ticket_id),
        )
        connection.execute("""
            INSERT INTO status_history
                (ticket_id, old_status, new_status, changed_by_staff_id)
            VALUES (?, ?, ?, 1)
        """, (ticket_id, old_status, status))
        connection.commit()

    connection.close()
    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.post("/staff/tickets/<int:ticket_id>/assign")
def assign_ticket(ticket_id):
    staff_id = request.form.get("assigned_staff_id", type=int)

    connection = connect_db()
    ticket = connection.execute(
        "SELECT id FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()

    if ticket is None:
        connection.close()
        return "Ticket not found", 404

    if staff_id and not connection.execute(
        "SELECT id FROM staff_users WHERE id = ?",
        (staff_id,),
    ).fetchone():
        connection.close()
        return "Staff member not found", 400

    connection.execute(
        "UPDATE tickets SET assigned_staff_id = ? WHERE id = ?",
        (staff_id, ticket_id),
    )
    connection.commit()
    connection.close()

    return redirect(url_for("ticket_detail", ticket_id=ticket_id))


@app.post("/staff/tickets/<int:ticket_id>/reply")
def add_ticket_reply(ticket_id):
    message = request.form.get("message", "").strip()

    if not message:
        return "Reply cannot be empty", 400

    connection = connect_db()

    ticket = connection.execute("""
        SELECT c.name, o.order_number
        FROM tickets t
        JOIN customers c ON c.id = t.customer_id
        JOIN orders o ON o.id = t.order_id
        WHERE t.id = ?
    """, (ticket_id,)).fetchone()

    if ticket is None:
        connection.close()
        return "Ticket not found", 404

    customer_name, order_number = ticket

    connection.execute("""
        INSERT INTO ticket_messages
            (ticket_id, sender_type, staff_user_id, message)
        VALUES (?, 'staff', 1, ?)
    """, (ticket_id, message))

    connection.commit()
    connection.close()

    send_reply_notification(ticket_id, customer_name, order_number, message)

    return redirect(url_for("ticket_detail", ticket_id=ticket_id))
@app.get("/report")
def weekly_report():
    connection = connect_db()

    rows = connection.execute("""
        WITH resolutions AS (
            SELECT ticket_id, MIN(changed_at) AS resolved_at
            FROM status_history
            WHERE new_status = 'resolved'
            GROUP BY ticket_id
        )
        SELECT
            t.issue_type,
            COUNT(*) AS ticket_count,
            ROUND(
                AVG(
                    CASE
                        WHEN r.resolved_at IS NOT NULL THEN
                            (julianday(r.resolved_at) - julianday(t.created_at)) * 24
                    END
                ),
                1
            ) AS average_resolution_hours
        FROM tickets t
        LEFT JOIN resolutions r ON r.ticket_id = t.id
        WHERE t.created_at >= datetime('now', '-7 days')
        GROUP BY t.issue_type
        ORDER BY ticket_count DESC
    """).fetchall()

    connection.close()

    issue_labels = {
        "late_delivery": "Late Delivery",
        "missing_item": "Missing Item",
        "damaged_item": "Damaged Item",
        "wrong_item": "Wrong Item",
        "billing_question": "Billing Question",
        "other": "Other",
    }

    return render_template(
        "report.html",
        rows=rows,
        issue_labels=issue_labels,
    )


if __name__ == "__main__":
    app.run(debug=True)
