import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("meridian.db")

connection = sqlite3.connect(DB_PATH)
connection.execute("PRAGMA foreign_keys = ON")

# Customers
connection.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL
    )
""")

# Pharmacies
connection.execute("""
    CREATE TABLE IF NOT EXISTS pharmacies (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        city TEXT NOT NULL
    )
""")

# Orders
connection.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY,
        order_number TEXT NOT NULL UNIQUE,
        customer_id INTEGER NOT NULL,
        pharmacy_id INTEGER NOT NULL,
        delivery_status TEXT NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (pharmacy_id) REFERENCES pharmacies(id)
    )
""")

# Support tickets
connection.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL,
        issue_type TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)
    )
""")
ticket_columns = [
    row[1] for row in connection.execute("PRAGMA table_info(tickets)")
]

if "photo_path" not in ticket_columns:
    connection.execute("ALTER TABLE tickets ADD COLUMN photo_path TEXT")

# Staff users
connection.execute("""
    CREATE TABLE IF NOT EXISTS staff_users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'support_agent'
    )
""")

# Ticket conversation messages
connection.execute("""
    CREATE TABLE IF NOT EXISTS ticket_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        sender_type TEXT NOT NULL
            CHECK (sender_type IN ('customer', 'staff')),
        staff_user_id INTEGER,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id),
        FOREIGN KEY (staff_user_id) REFERENCES staff_users(id)
    )
""")

# Ticket status changes
connection.execute("""
    CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        old_status TEXT,
        new_status TEXT NOT NULL,
        changed_by_staff_id INTEGER,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id),
        FOREIGN KEY (changed_by_staff_id) REFERENCES staff_users(id)
    )
""")

# Saved reply templates
connection.execute("""
    CREATE TABLE IF NOT EXISTS reply_templates (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        issue_type TEXT NOT NULL,
        body TEXT NOT NULL
    )
""")

# Synthetic customer, pharmacy, and orders
connection.execute(
    "INSERT OR IGNORE INTO customers (id, name) "
    "VALUES (1, 'Avery Johnson')"
)

connection.execute(
    "INSERT OR IGNORE INTO pharmacies (id, name, city) "
    "VALUES (1, 'Meridian Midtown Pharmacy', 'New York')"
)

connection.execute(
    "INSERT OR IGNORE INTO orders "
    "(id, order_number, customer_id, pharmacy_id, delivery_status) "
    "VALUES (1, 'MD-20481', 1, 1, 'In transit')"
)

connection.execute(
    "INSERT OR IGNORE INTO orders "
    "(id, order_number, customer_id, pharmacy_id, delivery_status) "
    "VALUES (2, 'MD-20482', 1, 1, 'Delivered')"
)

connection.execute(
    "INSERT INTO customers (name) "
    "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM customers WHERE name = ?)",
    ("Javid", "Javid"),
)

connection.executemany(
    """
    INSERT OR IGNORE INTO orders
        (order_number, customer_id, pharmacy_id, delivery_status)
    SELECT ?, id, 1, ?
    FROM customers
    WHERE id = (SELECT MIN(id) FROM customers WHERE name = 'Javid')
    """,
    [
        ("MD-20483", "In transit"),
        ("MD-20484", "Delivered"),
    ],
)

# Synthetic staff user
connection.execute(
    "INSERT OR IGNORE INTO staff_users (id, name, email) "
    "VALUES (1, 'Morgan Lee', 'morgan@example.com')"
)

# Add the assignee column to an existing tickets table if it is missing
ticket_columns = [
    row[1] for row in connection.execute("PRAGMA table_info(tickets)")
]

if "assigned_staff_id" not in ticket_columns:
    connection.execute("""
        ALTER TABLE tickets
        ADD COLUMN assigned_staff_id INTEGER
        REFERENCES staff_users(id)
    """)

# Add an initial status history entry for tickets without history
connection.execute("""
    INSERT INTO status_history (ticket_id, old_status, new_status)
    SELECT t.id, NULL, t.status
    FROM tickets t
    WHERE NOT EXISTS (
        SELECT 1
        FROM status_history h
        WHERE h.ticket_id = t.id
    )
""")

# Synthetic saved replies with customer and order placeholders
connection.executemany(
    """
    INSERT OR IGNORE INTO reply_templates
        (id, name, issue_type, body)
    VALUES (?, ?, ?, ?)
    """,
    [
        (
            1,
            "Late delivery update",
            "late_delivery",
            "Hi {customer_name}, we are checking the delivery status for order "
            "{order_number}. We will update you as soon as we have more information."
        ),
        (
            2,
            "Missing item follow-up",
            "missing_item",
            "Hi {customer_name}, we are checking your report about the missing item "
            "in order {order_number}. We will follow up shortly."
        ),
        (
            3,
            "Damaged item follow-up",
            "damaged_item",
            "Hi {customer_name}, we are reviewing your report about the damaged item "
            "in order {order_number}. We will follow up shortly."
        ),
        (
            4,
            "Wrong item follow-up",
            "wrong_item",
            "Hi {customer_name}, we are reviewing the wrong item reported for order "
            "{order_number}. We will follow up shortly."
        ),
        (
            5,
            "Billing question received",
            "billing_question",
            "Hi {customer_name}, we received your billing question about order "
            "{order_number}. We will review it and get back to you."
        ),
        (
            6,
            "General support follow-up",
            "other",
            "Hi {customer_name}, we received your message about order "
            "{order_number}. We will review it and follow up shortly."
        ),
    ]
)

connection.commit()
connection.close()

print("SETUP DATABASE:", DB_PATH.resolve())
print("Database tables and sample data are ready.")
