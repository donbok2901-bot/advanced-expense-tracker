import os
from flask import Flask, render_template, request, redirect, flash, session, jsonify, Response
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv
import pandas as pd
from datetime import datetime
import random
import difflib
import re

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")  # must match your verified SendGrid sender


def send_email(to_email, subject, body_text):
    """Sends an email via the SendGrid API (over HTTPS) instead of raw
    SMTP. Render's free tier blocks outbound SMTP ports (587/465), which
    caused password reset emails to hang and time out. HTTPS traffic
    isn't blocked, so this works both locally and in production."""
    api_key = os.getenv("SENDGRID_API_KEY")
    from_email = app.config["MAIL_USERNAME"]

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body_text}],
    }

    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    return response
serializer = URLSafeTimedSerializer(app.secret_key)

DEFAULT_CATEGORIES = [
    "Food", "Travel", "Shopping", "Bills", "Entertainment",
    "Health", "Groceries", "Rent", "Education", "Contact", "Other"
]

# Keyword -> category mapping used for auto-categorization.
# Matching is case-insensitive and checks if the keyword appears
# anywhere in the transaction description/merchant name.
CATEGORY_KEYWORDS = {
    "Food": ["zomato", "swiggy", "restaurant", "cafe", "dominos", "pizza",
             "mcdonald", "kfc", "starbucks", "food", "eatsure", "faasos",
             "behrouz", "box8", "freshmenu"],
    "Travel": ["uber", "ola", "irctc", "redbus", "flight", "airlines",
               "indigo", "train", "metro", "cab", "rapido", "makemytrip",
               "goibibo", "yatra", "cleartrip", "air india"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "mall", "shopping",
                 "meesho", "snapdeal", "nykaa", "tatacliq", "croma",
                 "reliance digital", "shopclues", "limeroad", "lenskart",
                 "firstcry", "purplle", "urban company"],
    "Bills": ["electricity", "water bill", "broadband", "recharge", "jio",
              "airtel", "vodafone", "vi ", "dth", "bill", "act fibernet",
              "bsnl", "gas bill", "postpaid"],
    "Entertainment": ["netflix", "prime video", "hotstar", "spotify",
                       "bookmyshow", "pvr", "inox", "movie", "youtube premium",
                       "sonyliv", "zee5", "gaana", "wynk"],
    "Groceries": ["bigbasket", "grofers", "blinkit", "zepto", "dmart",
                  "grocery", "supermarket", "jiomart", "nature's basket",
                  "spencer's", "more supermarket"],
    "Health": ["pharmacy", "hospital", "clinic", "apollo", "medplus", "medical",
               "netmeds", "pharmeasy", "1mg", "practo", "diagnostic"],
    "Rent": ["rent", "landlord", "housing.com", "nobroker"],
    "Education": ["udemy", "coursera", "tuition", "school", "college", "course",
                  "byju", "unacademy", "vedantu", "upgrad"],
}


def guess_category(description):
    """Returns a category name guessed from keywords in the description.
    Falls back to 'Other' if nothing matches.

    Uses two passes:
    1. Exact substring match (fast, precise) -- e.g. "zomato" in "Zomato Order"
    2. Fuzzy word match (catches typos/misspellings) -- e.g. "zomata" still
       resolves to Food, since it's a close match to "zomato"
    """
    if not description:
        return "Other"
    text = description.lower()

    # Pass 1: exact substring match
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return category

    # Pass 2: fuzzy match against individual words, to catch typos.
    # Only applied to keywords of 4+ characters to avoid false positives
    # on very short keywords (e.g. "vi ", "dth").
    words = re.findall(r"[a-zA-Z0-9']+", text)
    best_category = None
    best_ratio = 0.0

    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            kw_clean = kw.strip()
            if len(kw_clean) < 4:
                continue
            for word in words:
                if len(word) < 4:
                    continue
                ratio = difflib.SequenceMatcher(None, word, kw_clean).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_category = category

    # 0.8 similarity threshold: catches typos like "zomata" -> "zomato"
    # (ratio ~0.83) and "swigy" -> "swiggy" (ratio ~0.91), while still being
    # strict enough to avoid matching unrelated words.
    if best_ratio >= 0.8:
        return best_category

    return "Other"


def get_category_id(cursor, category_name):
    cursor.execute("SELECT id FROM categories WHERE name = %s", (category_name,))
    row = cursor.fetchone()
    return row[0] if row else None


def get_db_connection():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )
    return conn


def create_table():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL
        )
    """)

    # UPI PIN (hashed, never stored in plain text) and a simulated wallet
    # balance, so the payment flow can check "sufficient balance" before
    # allowing a payment -- since there's no real bank connection, this is
    # a demo balance that starts at a fixed amount and decreases as the
    # user makes payments through the app.
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_pin_hash VARCHAR(255)")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_balance NUMERIC(10, 2) DEFAULT 50000.00")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories(
            id SERIAL PRIMARY KEY,
            name VARCHAR(50) UNIQUE NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses(
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            amount NUMERIC(10, 2) NOT NULL,
            description VARCHAR(255),
            expense_date DATE NOT NULL,
            source VARCHAR(20) DEFAULT 'manual'
        )
    """)

    # Extra columns for richer transaction detail (payment app, payee ID,
    # a generated transaction reference, and a timestamp). Using
    # ADD COLUMN IF NOT EXISTS so this is safe to run against a table
    # that was already created before these columns existed.
    cursor.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS payment_app VARCHAR(50)")
    cursor.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS payee_id VARCHAR(100)")
    cursor.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS transaction_id VARCHAR(50)")
    cursor.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    # Simulated wallet balance + hashed UPI PIN for the mock payment flow.
    # This is a demo balance stored in our own database -- not a real bank
    # balance -- but it lets the "insufficient balance" / "check balance"
    # UX work realistically. New users start with a demo balance so the
    # feature has something to show immediately.
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_balance NUMERIC(12,2) DEFAULT 50000.00")
    cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_pin_hash VARCHAR(255)")

    for name in DEFAULT_CATEGORIES:
        cursor.execute(
            "INSERT INTO categories (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (name,)
        )

    print("Tables created successfully")
    conn.commit()
    cursor.close()
    conn.close()


# ------------------- Landing / Home -------------------

@app.route("/")
def landing():
    return render_template("front.html")


@app.route("/home")
def home():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()

    # This month's spending (used by the "This Month's Spending" stat card)
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) FROM expenses
        WHERE user_id = %s AND date_trunc('month', expense_date) = date_trunc('month', CURRENT_DATE)
    """, (session["user_id"],))
    monthly_spending = cursor.fetchone()[0]

    # Number of distinct categories actually used
    cursor.execute("""
        SELECT COUNT(DISTINCT category_id) FROM expenses
        WHERE user_id = %s AND category_id IS NOT NULL
    """, (session["user_id"],))
    categories_count = cursor.fetchone()[0]

    # Total number of transactions logged
    cursor.execute("""
        SELECT COUNT(*) FROM expenses WHERE user_id = %s
    """, (session["user_id"],))
    total_transactions = cursor.fetchone()[0]

    cursor.execute("""
        SELECT e.id, e.amount, e.description, e.expense_date, c.name
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = %s
        ORDER BY e.expense_date DESC, e.id DESC
        LIMIT 5
    """, (session["user_id"],))
    recent_expenses = cursor.fetchall()

    # Category-wise totals for this month, used to draw the dashboard pie chart
    cursor.execute("""
        SELECT COALESCE(c.name, 'Uncategorized') AS category_name, SUM(e.amount) AS total
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = %s AND date_trunc('month', e.expense_date) = date_trunc('month', CURRENT_DATE)
        GROUP BY category_name
        ORDER BY total DESC
    """, (session["user_id"],))
    category_rows = cursor.fetchall()
    category_labels = [row[0] for row in category_rows]
    category_totals = [float(row[1]) for row in category_rows]

    cursor.close()
    conn.close()

    return render_template(
        "home.html",
        monthly_spending=monthly_spending,
        categories_count=categories_count,
        total_transactions=total_transactions,
        recent_expenses=recent_expenses,
        category_labels=category_labels,
        category_totals=category_totals
    )


# ------------------- Register -------------------

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        hashed_password = generate_password_hash(password)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (name, email, password) VALUES (%s, %s, %s) RETURNING id",
                (name, email, hashed_password),
            )
            conn.commit()
            flash("Registration successful! Please log in.", "success")
            return redirect("/login")
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash("That email is already registered.", "danger")
            return render_template("register.html")
        finally:
            cursor.close()
            conn.close()

    return render_template("register.html")


# ------------------- Login -------------------

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, email, password FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and check_password_hash(user[3], password):
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            session["user_email"] = user[2]
            flash("Login successful!", "success")
            return redirect("/home")
        else:
            flash("Invalid email or password", "danger")
            return render_template("login.html")

    return render_template("login.html")


# ------------------- Logout -------------------

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect("/login")


# ------------------- Forgot Password -------------------

@app.route("/forget-password", methods=["GET", "POST"])
def forget_password():
    if request.method == "POST":
        email = request.form["email"]

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            token = serializer.dumps(email, salt="password-reset")
            # request.host_url gives the correct base URL whether running
            # locally (http://127.0.0.1:5000/) or on Render (https://your-app.onrender.com/)
            reset_link = f"{request.host_url}reset-password/{token}"

            body_text = f"""Hello,

You requested to reset your password.

Click the link below to reset it:
{reset_link}

This link will expire in 15 minutes.
If you did not request this, please ignore this email.

Expense Tracker Team
"""
            response = send_email(email, "Expense Tracker - Password Reset", body_text)

            if response.status_code in (200, 201, 202):
                flash("A password reset link has been sent to your email.", "success")
            else:
                flash("Could not send reset email right now. Please try again later.", "danger")

            return redirect("/login")
        else:
            flash("No account found with that email.", "danger")
            return render_template("forget_password.html")

    return render_template("forget_password.html")


# ------------------- Reset Password -------------------

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="password-reset", max_age=900)
    except Exception:
        flash("Invalid or expired reset link.", "danger")
        return redirect("/forget-password")

    if request.method == "POST":
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("reset_password.html", token=token)

        hashed_password = generate_password_hash(new_password)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = %s WHERE email = %s", (hashed_password, email))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Password updated! Please log in.", "success")
        return redirect("/login")

    return render_template("reset_password.html", token=token)


# ------------------- Add Expense -------------------

@app.route("/add-expense", methods=["GET", "POST"])
def add_expense():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        amount = request.form.get("amount")
        description = request.form.get("description")
        expense_date = request.form.get("expense_date")
        category_id = request.form.get("category_id")

        cursor.execute("""
            INSERT INTO expenses (user_id, category_id, amount, description, expense_date, source)
            VALUES (%s, %s, %s, %s, %s, 'manual')
        """, (session["user_id"], category_id or None, amount, description, expense_date))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Expense added!", "success")
        return redirect("/view-expenses")

    cursor.execute("SELECT id, name FROM categories ORDER BY name ASC")
    categories = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("add_expense.html", categories=categories)


# ------------------- View Expenses -------------------

@app.route("/view-expenses")
def view_expenses():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    start_date = request.args.get("start_date") or None
    end_date = request.args.get("end_date") or None

    query = """
        SELECT e.id, e.amount, e.description, e.expense_date, c.name, e.source
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = %s
    """
    params = [session["user_id"]]

    if start_date:
        query += " AND e.expense_date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND e.expense_date <= %s"
        params.append(end_date)

    query += " ORDER BY e.expense_date DESC, e.id DESC"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query, tuple(params))
    expenses = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template(
        "view_expenses.html",
        expenses=expenses,
        start_date=start_date,
        end_date=end_date
    )


# ------------------- Export Expenses (Date Range CSV Download) -------------------

@app.route("/export-expenses")
def export_expenses():
    """Streams a CSV download of the user's expenses, optionally filtered
    by a start_date / end_date query string range."""
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    start_date = request.args.get("start_date") or None
    end_date = request.args.get("end_date") or None

    query = """
        SELECT e.expense_date, e.description, c.name, e.amount, e.source,
               e.payment_app, e.payee_id, e.transaction_id
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.user_id = %s
    """
    params = [session["user_id"]]

    if start_date:
        query += " AND e.expense_date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND e.expense_date <= %s"
        params.append(end_date)

    query += " ORDER BY e.expense_date ASC, e.id ASC"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    df = pd.DataFrame(rows, columns=[
        "date", "description", "category", "amount", "source",
        "payment_app", "payee_id", "transaction_id"
    ])

    csv_data = df.to_csv(index=False)

    # Build a filename that reflects the selected range, if any
    if start_date and end_date:
        filename = f"expenses_{start_date}_to_{end_date}.csv"
    elif start_date:
        filename = f"expenses_from_{start_date}.csv"
    elif end_date:
        filename = f"expenses_until_{end_date}.csv"
    else:
        filename = "expenses_all.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ------------------- Edit Expense -------------------

@app.route("/edit-expense/<int:id>", methods=["GET", "POST"])
def edit_expense(id):
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        amount = request.form.get("amount")
        description = request.form.get("description")
        expense_date = request.form.get("expense_date")
        category_id = request.form.get("category_id")

        cursor.execute("""
            UPDATE expenses
            SET amount=%s, description=%s, expense_date=%s, category_id=%s
            WHERE id=%s AND user_id=%s
        """, (amount, description, expense_date, category_id or None, id, session["user_id"]))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Expense updated!", "success")
        return redirect("/view-expenses")

    cursor.execute("""
        SELECT id, amount, description, expense_date, category_id
        FROM expenses WHERE id = %s AND user_id = %s
    """, (id, session["user_id"]))
    expense = cursor.fetchone()

    cursor.execute("SELECT id, name FROM categories ORDER BY name ASC")
    categories = cursor.fetchall()

    cursor.close()
    conn.close()

    if not expense:
        flash("Expense not found.", "danger")
        return redirect("/view-expenses")

    return render_template("edit_expense.html", expense=expense, categories=categories)


# ------------------- Delete Expense -------------------

@app.route("/delete-expense/<int:id>")
def delete_expense(id):
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = %s AND user_id = %s", (id, session["user_id"]))
    conn.commit()
    cursor.close()
    conn.close()

    flash("Expense deleted.", "success")
    return redirect("/view-expenses")


# ------------------- CSV Import (Automatic Tracking) -------------------

@app.route("/recategorize-expenses")
def recategorize_expenses():
    """Re-runs auto-categorization on all of the current user's expenses.
    Useful after improving guess_category() (e.g. adding fuzzy matching),
    so existing entries like a typo'd 'zomata' get fixed retroactively."""
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, description, payee_id FROM expenses WHERE user_id = %s
    """, (session["user_id"],))
    rows = cursor.fetchall()

    updated_count = 0
    for expense_id, description, payee_id in rows:
        new_category = guess_category(description)
        if new_category == "Other" and payee_id:
            new_category = "Contact"
        new_category_id = get_category_id(cursor, new_category)
        cursor.execute("""
            UPDATE expenses SET category_id = %s WHERE id = %s AND user_id = %s
        """, (new_category_id, expense_id, session["user_id"]))
        updated_count += 1

    conn.commit()
    cursor.close()
    conn.close()

    flash(f"Re-checked {updated_count} transactions and updated their categories.", "success")
    return redirect("/view-expenses")


@app.route("/import-csv", methods=["GET", "POST"])
def import_csv():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    if request.method == "POST":
        file = request.files.get("csv_file")

        if not file or file.filename == "":
            flash("Please choose a CSV file to upload.", "danger")
            return redirect("/import-csv")

        try:
            df = pd.read_csv(file)
        except Exception as e:
            flash(f"Could not read that file as a CSV: {e}", "danger")
            return redirect("/import-csv")

        # Normalize column names to lowercase for flexible matching
        df.columns = [c.strip().lower() for c in df.columns]

        required_cols = {"date", "description", "amount"}
        if not required_cols.issubset(set(df.columns)):
            flash(
                f"CSV must have these columns: date, description, amount. "
                f"Found: {', '.join(df.columns)}",
                "danger"
            )
            return redirect("/import-csv")

        conn = get_db_connection()
        cursor = conn.cursor()

        imported_count = 0
        skipped_count = 0

        for _, row in df.iterrows():
            try:
                raw_date = str(row["date"])
                # Try a couple of common date formats
                expense_date = None
                for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
                    try:
                        expense_date = datetime.strptime(raw_date, fmt).date()
                        break
                    except ValueError:
                        continue
                if expense_date is None:
                    skipped_count += 1
                    continue

                description = str(row["description"])
                amount = float(row["amount"])

                category_name = guess_category(description)
                category_id = get_category_id(cursor, category_name)

                cursor.execute("""
                    INSERT INTO expenses (user_id, category_id, amount, description, expense_date, source)
                    VALUES (%s, %s, %s, %s, %s, 'csv')
                """, (session["user_id"], category_id, amount, description, expense_date))

                imported_count += 1
            except Exception:
                skipped_count += 1
                continue

        conn.commit()
        cursor.close()
        conn.close()

        flash(f"Imported {imported_count} transactions "
              f"({skipped_count} skipped due to formatting issues).", "success")
        return redirect("/view-expenses")

    return render_template("import_csv.html")


# ------------------- Simulated Payment Webhook (Demo) -------------------

# This demonstrates the architecture that WOULD be used if Google Pay /
# Paytm / YONO exposed a real webhook API to third-party apps. Since they
# don't, this route simulates what that incoming request would look like,
# so the auto-categorization + auto-insert pipeline can be demoed live.

SAMPLE_MERCHANTS = [
    ("Zomato", "Google Pay"),
    ("Swiggy", "Paytm"),
    ("Uber", "Google Pay"),
    ("Amazon", "YONO SBI"),
    ("BigBasket", "Paytm"),
    ("Netflix", "Google Pay"),
    ("Jio Recharge", "YONO SBI"),
]


@app.route("/simulate-payment", methods=["POST"])
def simulate_payment():
    """Simulates an incoming payment notification, as if a UPI app had
    sent a real webhook to our server the moment a payment was made.

    Accepts an optional JSON body: {merchant, amount, app_name}.
    If not provided, picks a random sample transaction instead."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}

    merchant = data.get("merchant")
    amount = data.get("amount")
    app_name = data.get("app_name")
    payee_id = data.get("payee_id")
    pin = data.get("pin", "")

    if not merchant or not amount:
        merchant, app_name = random.choice(SAMPLE_MERCHANTS)
        amount = round(random.uniform(50, 1500), 2)

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    # Verify the UPI PIN and check sufficient balance BEFORE inserting
    # anything -- this mirrors how a real payment would be authorized
    # and validated before the transaction is allowed to go through.
    pin_hash, wallet_balance = get_user_pin_and_balance(cursor, session["user_id"])

    if not pin_hash:
        cursor.close()
        conn.close()
        return jsonify({
            "error": "no_pin",
            "message": "You haven't set up a UPI PIN yet."
        }), 403

    if not check_password_hash(pin_hash, pin):
        cursor.close()
        conn.close()
        return jsonify({
            "error": "invalid_pin",
            "message": "Incorrect UPI PIN."
        }), 403

    wallet_balance = float(wallet_balance)
    if amount > wallet_balance:
        cursor.close()
        conn.close()
        return jsonify({
            "error": "insufficient_balance",
            "message": f"Insufficient balance. Available: ₹{wallet_balance:.2f}",
            "balance": wallet_balance
        }), 402

    expense_date = datetime.now().date()
    category_name = guess_category(merchant)

    # If the merchant name didn't match any known business keyword, but a
    # UPI ID or phone number WAS provided, this is most likely a payment to
    # a person (a friend, a local shop with no recognizable brand, etc.)
    # rather than a business -- show it as "Contact" instead of "Other".
    if category_name == "Other" and payee_id:
        category_name = "Contact"

    # Generate a realistic-looking transaction reference, e.g. TXN12345678ABCD
    transaction_id = "TXN" + datetime.now().strftime("%y%m%d%H%M%S") + \
        str(random.randint(100, 999))

    category_id = get_category_id(cursor, category_name)

    cursor.execute("""
        INSERT INTO expenses
            (user_id, category_id, amount, description, expense_date,
             source, payment_app, payee_id, transaction_id)
        VALUES (%s, %s, %s, %s, %s, 'webhook', %s, %s, %s)
        RETURNING id
    """, (session["user_id"], category_id, amount, merchant, expense_date,
          app_name, payee_id, transaction_id))
    new_id = cursor.fetchone()[0]

    new_balance = wallet_balance - amount
    cursor.execute("""
        UPDATE users SET wallet_balance = %s WHERE id = %s
    """, (new_balance, session["user_id"]))

    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({
        "id": new_id,
        "app": app_name,
        "transaction_id": transaction_id,
        "merchant": merchant,
        "amount": amount,
        "category": category_name,
        "date": str(expense_date),
        "balance": new_balance
    })


# ------------------- UPI PIN Setup / Balance / Payment Auth -------------------

# NOTE ON SECURITY: this is a DEMO feature for a student project. The PIN
# is hashed (never stored in plain text) using the same method as account
# passwords, which is good practice regardless -- but this is still a
# simulated wallet, not a real bank connection or a production-grade
# payment security system. Real UPI PINs are verified by banks/NPCI
# through hardware-backed secure channels, which isn't something a
# student web app can (or should attempt to) replicate.

def get_user_pin_and_balance(cursor, user_id):
    cursor.execute("""
        SELECT upi_pin_hash, wallet_balance FROM users WHERE id = %s
    """, (user_id,))
    return cursor.fetchone()


@app.route("/setup-upi-pin", methods=["GET", "POST"])
def setup_upi_pin():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    pin_hash, wallet_balance = get_user_pin_and_balance(cursor, session["user_id"])

    if pin_hash:
        cursor.close()
        conn.close()
        flash("You already have a UPI PIN set up.", "success")
        return redirect("/mock-payment")

    if request.method == "POST":
        pin = request.form.get("pin", "")
        confirm_pin = request.form.get("confirm_pin", "")

        if not re.fullmatch(r"\d{4,6}", pin):
            flash("PIN must be 4 to 6 digits.", "danger")
            cursor.close()
            conn.close()
            return render_template("setup_upi_pin.html")

        if pin != confirm_pin:
            flash("PINs do not match.", "danger")
            cursor.close()
            conn.close()
            return render_template("setup_upi_pin.html")

        pin_hash = generate_password_hash(pin)
        cursor.execute("""
            UPDATE users SET upi_pin_hash = %s WHERE id = %s
        """, (pin_hash, session["user_id"]))
        conn.commit()
        cursor.close()
        conn.close()

        flash("UPI PIN set up successfully!", "success")
        return redirect("/mock-payment")

    cursor.close()
    conn.close()
    return render_template("setup_upi_pin.html")


@app.route("/check-balance", methods=["GET", "POST"])
def check_balance():
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    pin_hash, wallet_balance = get_user_pin_and_balance(cursor, session["user_id"])

    if not pin_hash:
        cursor.close()
        conn.close()
        flash("Please set up a UPI PIN first.", "danger")
        return redirect("/setup-upi-pin")

    balance_result = None

    if request.method == "POST":
        pin = request.form.get("pin", "")
        if check_password_hash(pin_hash, pin):
            balance_result = float(wallet_balance)
        else:
            flash("Incorrect PIN.", "danger")

    cursor.close()
    conn.close()
    return render_template("check_balance.html", balance=balance_result)


@app.route("/verify-upi-pin", methods=["POST"])
def verify_upi_pin():
    """AJAX endpoint: checks whether a submitted PIN is correct, and
    whether the user has a PIN set up at all. Used by the payment flow
    before allowing a transaction to proceed."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    pin = data.get("pin", "")

    conn = get_db_connection()
    cursor = conn.cursor()
    pin_hash, wallet_balance = get_user_pin_and_balance(cursor, session["user_id"])
    cursor.close()
    conn.close()

    if not pin_hash:
        return jsonify({"has_pin": False, "valid": False})

    valid = check_password_hash(pin_hash, pin)
    return jsonify({
        "has_pin": True,
        "valid": valid,
        "balance": float(wallet_balance) if valid else None
    })


# ------------------- Mock Payment App Screen (Demo) -------------------


@app.route("/mock-payment")
def mock_payment():
    """Renders a mock UPI payment app screen. When the user hits 'Pay',
    the frontend JS calls /simulate-payment with the entered details,
    which flows into the expense tracker automatically."""
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    return render_template("mock_payment.html")


# ------------------- Transaction Detail -------------------

@app.route("/transaction/<int:id>")
def transaction_detail(id):
    """Shows full details for a single expense/transaction, including
    payment app, payee ID, and transaction reference for webhook-sourced
    (simulated payment) entries."""
    if "user_id" not in session:
        flash("Please log in first.", "danger")
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT e.id, e.amount, e.description, e.expense_date, c.name,
               e.source, e.payment_app, e.payee_id, e.transaction_id, e.created_at
        FROM expenses e
        LEFT JOIN categories c ON e.category_id = c.id
        WHERE e.id = %s AND e.user_id = %s
    """, (id, session["user_id"]))
    txn = cursor.fetchone()
    cursor.close()
    conn.close()

    if not txn:
        flash("Transaction not found.", "danger")
        return redirect("/view-expenses")

    return render_template("transaction_detail.html", txn=txn)


# Run once when this module is imported -- whether that's via
# `python app.py` locally, or via gunicorn ("gunicorn app:app") in
# production. If this were only inside the __main__ block below, gunicorn
# would never call it, since gunicorn imports the app rather than running
# it as a script -- which is exactly what caused the "relation users
# does not exist" error on Render.
create_table()


if __name__ == "__main__":
    app.run(debug=True)
