from flask import Flask, render_template, request, redirect, url_for, flash, Response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import qrcode
import os
import time
import logging
from datetime import datetime
import pandas as pd
from contextlib import closing
import smtplib
from email.mime.text import MIMEText

# Create the Flask app with instance-relative config enabled
app = Flask(__name__, instance_relative_config=True)
app.secret_key = os.urandom(24)

# Ensure the instance folder exists
try:
    os.makedirs(app.instance_path, exist_ok=True)
except OSError as e:
    logging.critical("Could not create instance folder: %s", str(e))
    raise

# Configure logging with a detailed format and level
logging.basicConfig(
    filename='nysc_server.log',
    level=logging.DEBUG,  # Capture detailed logs
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Admin credentials (update in production and consider using environment variables)
ADMIN_USER = 'admin'
ADMIN_PASSWORD_HASH = generate_password_hash('nysc_admin_password')

# Database configuration: store the database file in the instance folder
DATABASE_PATH = os.path.join(app.instance_path, 'nysc_accounts.db')
BACKUP_DIR = os.path.join(app.instance_path, 'backups')

def get_db(retries=3, delay=1):
    """Get a database connection with retries and error handling."""
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            logging.debug("Database connection established on attempt %d.", attempt + 1)
            return conn
        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e):
                logging.error("Database connection failed (attempt %d): %s", attempt + 1, str(e))
                if attempt < retries - 1:
                    time.sleep(delay)
            else:
                raise
    error_msg = f"Failed to connect to database after {retries} attempts"
    logging.critical(error_msg)
    raise Exception(error_msg)

def init_db():
    """Initialize the database with error handling."""
    try:
        db_dir = os.path.dirname(DATABASE_PATH)
        os.makedirs(db_dir, exist_ok=True)
        with closing(get_db()) as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_code TEXT NOT NULL,
                    corps_member_name TEXT NOT NULL,
                    sex TEXT NOT NULL,
                    bank_name TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    phone_number TEXT NOT NULL,
                    callup_number TEXT,
                    callup_letter_name TEXT,
                    account_name TEXT,
                    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            db.commit()
        logging.info("Database initialized successfully.")
    except Exception as e:
        logging.critical("Database initialization failed: %s", str(e), exc_info=True)
        raise

def generate_qr_code():
    """Generate QR code with fallback for file permission errors."""
    try:
        # Replace YOUR_LOCAL_IP with your actual LAN IP (e.g., "192.168.1.100")
        form_url = "http://YOUR_LOCAL_IP:5000/form"  
        qr = qrcode.make(form_url)
        static_folder = os.path.join(app.root_path, 'static')
        os.makedirs(static_folder, exist_ok=True)
        qr.save(os.path.join(static_folder, "qr_code.png"))
        logging.info("QR code generated and saved successfully.")
    except PermissionError:
        logging.error("Permission denied: Could not save QR code to 'static' folder")
    except Exception as e:
        logging.error("QR code generation failed: %s", str(e), exc_info=True)

def backup_db():
    """Backup database with error handling."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup_name = os.path.join(BACKUP_DIR, f'nysc_backup_{timestamp}.db')
        with open(DATABASE_PATH, 'rb') as src, open(backup_name, 'wb') as dst:
            dst.write(src.read())
        logging.info("Database backup created successfully: %s", backup_name)
    except Exception as e:
        logging.error("Backup failed: %s", str(e), exc_info=True)

# ------------------ User Routes (Form, Preview, Submission) ------------------

@app.route('/form', methods=['GET'])
def display_form():
    return render_template('form.html')

@app.route('/preview', methods=['POST'])
def preview():
    try:
        # Get and normalize all form data to uppercase as needed.
        data = {
            "state_code": request.form.get("state_code", "").upper(),
            "corps_member_name": request.form.get("corps_member_name", "").upper(),
            "sex": request.form.get("sex", "").upper(),  # if you want "MALE"/"FEMALE"
            "bank_name": request.form.get("bank_name", "").upper(),
            "account_number": request.form.get("account_number", "").upper(),
            "phone_number": request.form.get("phone_number", "").upper(),
            "callup_number": request.form.get("callup_number", "").upper(),
            "callup_letter_name": request.form.get("callup_letter_name", "").upper(),
            "account_name": request.form.get("account_name", "").upper()
        }
        logging.debug("Preview data: %s", data)
        return render_template('preview.html', data=data)
    except Exception as e:
        logging.error("Error during preview: %s", str(e), exc_info=True)
        flash('An error occurred during preview. Please try again.', 'danger')
        return redirect(url_for('display_form')), 500


@app.route('/submit', methods=['POST'])
def submit():
    try:
        # Normalize data to uppercase.
        data = {
            "state_code": request.form.get("state_code", "").upper(),
            "corps_member_name": request.form.get("corps_member_name", "").upper(),
            "sex": request.form.get("sex", "").upper(),
            "bank_name": request.form.get("bank_name", "").upper(),
            "account_number": request.form.get("account_number", "").upper(),
            "phone_number": request.form.get("phone_number", "").upper(),
            "callup_number": request.form.get("callup_number", "").upper(),
            "callup_letter_name": request.form.get("callup_letter_name", "").upper(),
            "account_name": request.form.get("account_name", "").upper()
        }
        with closing(get_db()) as db:
            # Check for duplicate account numbers
            existing = db.execute(
                'SELECT * FROM submissions WHERE account_number=?',
                (data["account_number"],)
            ).fetchone()
            if existing:
                logging.warning("Duplicate submission attempted for account number: %s", data["account_number"])
                flash('Error: Account number already exists!', 'danger')
                return redirect(url_for('display_form'))
            db.execute('''
                INSERT INTO submissions 
                (state_code, corps_member_name, sex, bank_name, account_number, phone_number, callup_number, callup_letter_name, account_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data["state_code"],
                data["corps_member_name"],
                data["sex"],
                data["bank_name"],
                data["account_number"],
                data["phone_number"],
                data["callup_number"],
                data["callup_letter_name"],
                data["account_name"]
            ))
            db.commit()
        logging.info("New registration added for %s.", data["corps_member_name"])
        flash('Registration successful!', 'success')
        return redirect(url_for('display_form'))
    except Exception as e:
        logging.error("Error during submission: %s", str(e), exc_info=True)
        flash('A system error occurred. Please try again later.', 'danger')
        return redirect(url_for('display_form')), 500

# ------------------ Admin Section ------------------

login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

class User(UserMixin):
    pass

@login_manager.user_loader
def load_user(user_id):
    if user_id == ADMIN_USER:
        user = User()
        user.id = ADMIN_USER
        return user
    return None

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, password):
            user = User()
            user.id = username
            login_user(user)
            logging.info("Admin '%s' logged in successfully.", username)
            return redirect(url_for('admin_dashboard'))
        logging.warning("Failed login attempt for username: %s", username)
        flash('Invalid credentials!', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    logging.info("Admin logged out.")
    flash('Logged out successfully.', 'success')
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    try:
        with closing(get_db()) as db:
            submissions = db.execute('SELECT * FROM submissions ORDER BY submitted_at DESC').fetchall()
        return render_template('admin_dashboard.html', submissions=submissions)
    except Exception as e:
        logging.error("Error loading admin dashboard: %s", str(e), exc_info=True)
        flash('Unable to load dashboard at this time.', 'danger')
        return render_template('admin_dashboard.html', submissions=[]), 500

@app.route('/admin/search', methods=['POST'])
@login_required
def search():
    try:
        query = f"%{request.form['query']}%"
        with closing(get_db()) as db:
            results = db.execute('''
                SELECT * FROM submissions 
                WHERE corps_member_name LIKE ? OR state_code LIKE ? OR account_number LIKE ?
            ''', (query, query, query)).fetchall()
        logging.info("Search performed with query: %s", request.form['query'])
        return render_template('admin_dashboard.html', submissions=results)
    except Exception as e:
        logging.error("Error during search: %s", str(e), exc_info=True)
        flash('An error occurred during the search. Please try again later.', 'danger')
        return redirect(url_for('admin_dashboard')), 500

@app.route('/admin/delete/<int:id>')
@login_required
def delete(id):
    try:
        with closing(get_db()) as db:
            db.execute('DELETE FROM submissions WHERE id = ?', (id,))
            db.commit()
        logging.info("Record with id %d deleted successfully.", id)
        flash('Record deleted successfully!', 'success')
        return redirect(url_for('admin_dashboard'))
    except Exception as e:
        logging.error("Error deleting record with id %d: %s", id, str(e), exc_info=True)
        flash('An error occurred while deleting the record. Please try again later.', 'danger')
        return redirect(url_for('admin_dashboard')), 500

@app.route('/admin/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit(id):
    if request.method == 'POST':
        data = request.form
        try:
            with closing(get_db()) as db:
                db.execute('''
                    UPDATE submissions 
                    SET state_code = ?, corps_member_name = ?, sex = ?, bank_name = ?, account_number = ?, phone_number = ?, callup_number = ?, callup_letter_name = ?, account_name = ?
                    WHERE id = ?
                ''', (
                    data["state_code"].upper(),
                    data["corps_member_name"].upper(),
                    data["sex"].upper(),
                    data["bank_name"].upper(),
                    data["account_number"].upper(),
                    data["phone_number"].upper(),
                    data["callup_number"].upper(),
                    data["callup_letter_name"].upper(),
                    data["account_name"].upper(),
                    id
                ))
                db.commit()
            logging.info("Record with id %d updated successfully.", id)
            flash('Update successful!', 'success')
            return redirect(url_for('admin_dashboard'))
        except Exception as e:
            logging.error("Error updating record with id %d: %s", id, str(e), exc_info=True)
            flash('An error occurred while updating the record. Please try again later.', 'danger')
            return redirect(url_for('admin_dashboard')), 500

    try:
        with closing(get_db()) as db:
            submission = db.execute('SELECT * FROM submissions WHERE id = ?', (id,)).fetchone()
        if submission is None:
            logging.warning("Record with id %d not found for editing.", id)
            flash('Record not found.', 'warning')
            return redirect(url_for('admin_dashboard')), 404
        return render_template('edit.html', submission=submission)
    except Exception as e:
        logging.error("Error loading edit page for record with id %d: %s", id, str(e), exc_info=True)
        flash('An error occurred. Please try again later.', 'danger')
        return redirect(url_for('admin_dashboard')), 500


@app.route('/admin/export')
@login_required
def export_to_excel():
    try:
        with closing(get_db()) as db:
            df = pd.read_sql_query('SELECT * FROM submissions', db)
        filename = os.path.join(app.instance_path, f"nysc_export_{datetime.now().strftime('%Y-%m-%d')}.xlsx")
        df.to_excel(filename, index=False)
        logging.info("Data exported to Excel file: %s", filename)
        flash(f'Export successful! Data has been saved to {filename}.', 'success')
    except Exception as e:
        logging.error("Export failed: %s", str(e), exc_info=True)
        flash(f'Export failed: {str(e)}. Please check the server logs for more details.', 'danger')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/backups')
@login_required
def list_backups():
    try:
        # Get all .db files from the BACKUP_DIR
        backup_files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')], reverse=True)
        logging.info("Listing %d backup files.", len(backup_files))
        return render_template('admin_backups.html', backups=backup_files)
    except Exception as e:
        logging.error("Error listing backups: %s", str(e), exc_info=True)
        flash('An error occurred while listing backups.', 'danger')
        return redirect(url_for('admin_dashboard')), 500


def get_backup_db(backup_filename):
    # Ensure the file is within the backup directory
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    if not os.path.exists(backup_path):
        raise Exception("Backup file does not exist.")
    conn = sqlite3.connect(backup_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/admin/backup/<backup_filename>')
@login_required
def view_backup(backup_filename):
    try:
        with closing(get_backup_db(backup_filename)) as db:
            submissions = db.execute('SELECT * FROM submissions ORDER BY submitted_at DESC').fetchall()
        return render_template('admin_backup_dashboard.html', submissions=submissions, backup_filename=backup_filename)
    except Exception as e:
        logging.error("Error viewing backup '%s': %s", backup_filename, str(e), exc_info=True)
        flash('An error occurred while loading the backup.', 'danger')
        return redirect(url_for('list_backups')), 500

@app.route('/admin/backup/download/<backup_filename>/<int:id>')
@login_required
def download_backup_record(backup_filename, id):
    try:
        with closing(get_backup_db(backup_filename)) as db:
            record = db.execute('SELECT * FROM submissions WHERE id=?', (id,)).fetchone()
        if record is None:
            flash('Record not found in backup.', 'warning')
            return redirect(url_for('view_backup', backup_filename=backup_filename))
        content = f"""Record Details:
ID: {record['id']}
State Code: {record['state_code']}
Name: {record['corps_member_name']}
Sex: {record['sex']}
Bank Name: {record['bank_name']}
Account Number: {record['account_number']}
Phone Number: {record['phone_number']}
Callup Number: {record['callup_number']}
Name on Call-up Letter: {record['callup_letter_name']}
Account Name: {record['account_name']}
Submitted At: {record['submitted_at']}
"""
        response = Response(content, mimetype='text/plain')
        response.headers['Content-Disposition'] = f'attachment; filename=backup_{backup_filename}_record_{id}.txt'
        logging.info("Backup record %d from %s downloaded successfully.", id, backup_filename)
        return response
    except Exception as e:
        logging.error("Error downloading backup record %d from %s: %s", id, backup_filename, str(e), exc_info=True)
        flash('An error occurred during download.', 'danger')
        return redirect(url_for('view_backup', backup_filename=backup_filename)), 500

# -------------- New Admin Functionalities --------------

# Download: Download an individual record as a plain text file
@app.route('/admin/download/<int:id>')
@login_required
def download_record(id):
    try:
        with closing(get_db()) as db:
            record = db.execute('SELECT * FROM submissions WHERE id=?', (id,)).fetchone()
        if record is None:
            flash('Record not found.', 'warning')
            return redirect(url_for('admin_dashboard'))
        content = f"""Record Details:
ID: {record['id']}
State Code: {record['state_code']}
Name: {record['corps_member_name']}
Sex: {record['sex']}
Bank Name: {record['bank_name']}
Account Number: {record['account_number']}
Phone Number: {record['phone_number']}
Callup Number: {record['callup_number']}
Name on Call-up Letter: {record['callup_letter_name']}
Account Name: {record['account_name']}
Submitted At: {record['submitted_at']}
"""
        response = Response(content, mimetype='text/plain')
        response.headers['Content-Disposition'] = f'attachment; filename=record_{id}.txt'
        logging.info("Record %d downloaded successfully.", id)
        return response
    except Exception as e:
        logging.error("Error downloading record %d: %s", id, str(e), exc_info=True)
        flash('An error occurred during download.', 'danger')
        return redirect(url_for('admin_dashboard')), 500
    

# Print: Display a print-friendly view of a record
@app.route('/admin/print/<int:id>')
@login_required
def print_record(id):
    try:
        with closing(get_db()) as db:
            record = db.execute('SELECT * FROM submissions WHERE id=?', (id,)).fetchone()
        if record is None:
            flash('Record not found.', 'warning')
            return redirect(url_for('admin_dashboard'))
        return render_template('print_record.html', record=record)
    except Exception as e:
        logging.error("Error preparing print view for record %d: %s", id, str(e), exc_info=True)
        flash('An error occurred while preparing print view.', 'danger')
        return redirect(url_for('admin_dashboard')), 500

# Email Configuration (Update these with real credentials for production)
EMAIL_HOST = 'smtp.example.com'
EMAIL_PORT = 587
EMAIL_USER = 'your_email@example.com'
EMAIL_PASS = 'your_email_password'

def send_email(recipient, subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_USER
    msg['To'] = recipient
    try:
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [recipient], msg.as_string())
        server.quit()
        logging.info("Email sent to %s", recipient)
    except Exception as e:
        logging.error("Error sending email to %s: %s", recipient, str(e), exc_info=True)
        raise

# Forward: Forward a record via email
@app.route('/admin/forward/<int:id>', methods=['GET', 'POST'])
@login_required
def forward_record(id):
    try:
        with closing(get_db()) as db:
            record = db.execute('SELECT * FROM submissions WHERE id=?', (id,)).fetchone()
        if record is None:
            flash('Record not found.', 'warning')
            return redirect(url_for('admin_dashboard'))
        if request.method == 'POST':
            recipient = request.form.get('recipient')
            if not recipient:
                flash('Please provide an email address.', 'warning')
                return redirect(url_for('forward_record', id=id))
            subject = f"Forwarded Record Details for {record['corps_member_name']}"
            body = f"""Record Details:
ID: {record['id']}
State Code: {record['state_code']}
Name: {record['corps_member_name']}
Sex: {record['sex']}
Bank Name: {record['bank_name']}
Account Number: {record['account_number']}
Phone Number: {record['phone_number']}
Callup Number: {record['callup_number']}
Name on Call-up Letter: {record['callup_letter_name']}
Account Name: {record['account_name']}
Submitted At: {record['submitted_at']}
"""
            try:
                send_email(recipient, subject, body)
                flash('Record forwarded successfully.', 'success')
            except Exception as e:
                flash('Failed to send email.', 'danger')
            return redirect(url_for('admin_dashboard'))
        # GET: Render the forwarding form
        return render_template('forward.html', record=record)
    except Exception as e:
        logging.error("Error forwarding record %d: %s", id, str(e), exc_info=True)
        flash('An error occurred while forwarding the record.', 'danger')
        return redirect(url_for('admin_dashboard')), 500

# ------------------ Global Error Handlers ------------------

@app.errorhandler(404)
def not_found_error(error):
    logging.warning("404 Not Found: %s", error)
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error("500 Internal Server Error: %s", error, exc_info=True)
    return render_template('500.html'), 500

# ------------------ Server Startup ------------------

if __name__ == '__main__':
    try:
        init_db()
        generate_qr_code()
        backup_db()  # Consider scheduling backups separately in production.
        app.run(host='0.0.0.0', port=5000, threaded=True)
    except Exception as e:
        logging.critical("Server startup failed: %s", str(e), exc_info=True)
        print(f"Critical error: {str(e)}. Check logs/nysc_server.log")
