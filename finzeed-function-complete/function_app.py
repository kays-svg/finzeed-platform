import azure.functions as func
import logging
import json
import os
import pyodbc
from datetime import datetime, timedelta, timezone
from azure.storage.blob import BlobServiceClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import io
import re
import base64
import hashlib
import secrets
import requests  # Added for OpenAI API calls

try:
    import jwt  # PyJWT
except ImportError:
    jwt = None

try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
except ImportError:
    SendGridAPIClient = None
    Mail = None

try:
    import anthropic as anthropic_sdk
except ImportError:
    anthropic_sdk = None

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Initialize Azure clients
blob_service_client = BlobServiceClient.from_connection_string(
    os.environ.get("AzureWebJobsStorage")
)

# Document Intelligence client
doc_intel_endpoint = os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT")
doc_intel_key = os.environ.get("DOCUMENT_INTELLIGENCE_KEY")
document_analysis_client = DocumentAnalysisClient(
    endpoint=doc_intel_endpoint,
    credential=AzureKeyCredential(doc_intel_key)
) if doc_intel_endpoint and doc_intel_key else None

pyodbc.pooling = True  # Enable built-in ODBC connection pooling

def get_db_connection():
    """Get database connection with ODBC pooling and retry for serverless DB auto-pause"""
    import time
    connection_string = os.environ.get('SQL_CONNECTION_STRING')
    if not connection_string:
        raise Exception("SQL_CONNECTION_STRING not found in environment variables")

    last_err = None
    for attempt in range(3):
        try:
            return pyodbc.connect(connection_string)
        except (pyodbc.OperationalError, pyodbc.Error) as e:
            last_err = e
            logging.warning(f"DB connection attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise last_err


# ========== DB KEEP-ALIVE TIMER ==========
@app.timer_trigger(schedule="0 */45 * * * *", arg_name="timer", run_on_startup=False)
def db_keepalive(timer: func.TimerRequest) -> None:
    """Ping the database every 45 minutes to prevent serverless auto-pause"""
    try:
        conn = pyodbc.connect(os.environ.get('SQL_CONNECTION_STRING', ''))
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        logging.info("DB keep-alive ping successful")
    except Exception as e:
        logging.warning(f"DB keep-alive ping failed: {e}")

# ========== AUTH UTILITIES ==========

def hash_password(password, salt=None):
    """Hash password with PBKDF2-SHA256 and random salt"""
    if salt is None:
        salt = secrets.token_hex(32)
    pw_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return pw_hash, salt

def create_jwt_token(user_id, email):
    """Create a JWT token with 7-day expiry"""
    if not jwt:
        return None
    secret = os.environ.get('JWT_SECRET')
    if not secret:
        logging.error("JWT_SECRET not configured — cannot issue tokens")
        return None
    payload = {
        'user_id': user_id,
        'email': email,
        'exp': datetime.now(timezone.utc) + timedelta(days=7),
        'iat': datetime.now(timezone.utc)
    }
    return jwt.encode(payload, secret, algorithm='HS256')

def verify_jwt_token(req):
    """Verify JWT from Authorization header. Returns payload or None."""
    if not jwt:
        return None
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:]
    try:
        secret = os.environ.get('JWT_SECRET')
        if not secret:
            return None
        return jwt.decode(token, secret, algorithms=['HS256'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

def make_response(data, status_code=200):
    """Helper to create JSON response with CORS headers"""
    return func.HttpResponse(
        json.dumps(data),
        status_code=status_code,
        mimetype="application/json",
        headers={'Access-Control-Allow-Origin': '*'}
    )

def ensure_auth_columns(cursor):
    """Add password and verification columns to users table if they don't exist"""
    try:
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'password_hash'
            )
            BEGIN
                ALTER TABLE users ADD password_hash NVARCHAR(256) NULL;
                ALTER TABLE users ADD password_salt NVARCHAR(64) NULL;
            END;
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'email_verified'
            )
            BEGIN
                ALTER TABLE users ADD email_verified BIT DEFAULT 0;
                ALTER TABLE users ADD verification_token NVARCHAR(128) NULL;
            END
        """)
        cursor.commit()
    except Exception as e:
        logging.warning(f"Auth columns check: {str(e)}")

def send_verification_email(email, token, firstname):
    """Send verification email via SendGrid"""
    sendgrid_key = os.environ.get('SENDGRID_API_KEY')
    sender_email = os.environ.get('SENDGRID_SENDER_EMAIL', 'noreply@finzeed.com')
    site_url = os.environ.get('SITE_URL', 'https://ashy-tree-0dadc2203.4.azurestaticapps.net')

    if not sendgrid_key or not SendGridAPIClient:
        logging.warning("SendGrid not configured — skipping verification email")
        return False

    verify_link = f"{site_url}?verify={token}"

    import html as html_module
    safe_name = html_module.escape(firstname)

    html_content = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:2rem;">
      <div style="text-align:center;margin-bottom:2rem;">
        <div style="display:inline-block;background:linear-gradient(135deg,#0a7c1a,#10b530);color:white;
                    width:48px;height:48px;border-radius:12px;line-height:48px;font-weight:800;font-size:1rem;">FZ</div>
        <h2 style="color:#0a7c1a;margin-top:0.5rem;">Finzeed</h2>
      </div>
      <h3 style="color:#0e1a11;">Welcome, {safe_name}!</h3>
      <p style="color:#556b5e;line-height:1.6;">
        Thank you for creating your Finzeed account. Please verify your email address by clicking the button below.
      </p>
      <div style="text-align:center;margin:2rem 0;">
        <a href="{verify_link}"
           style="display:inline-block;background:#0a7c1a;color:white;padding:0.8rem 2rem;
                  border-radius:10px;text-decoration:none;font-weight:600;font-size:1rem;">
          Verify my email
        </a>
      </div>
      <p style="color:#8a9b90;font-size:0.85rem;">
        If the button doesn't work, copy and paste this link:<br>
        <a href="{verify_link}" style="color:#0a7c1a;">{verify_link}</a>
      </p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:2rem 0;">
      <p style="color:#8a9b90;font-size:0.8rem;text-align:center;">
        Finzeed — AI-powered working capital financing for Egyptian SMEs
      </p>
    </div>
    """

    message = Mail(
        from_email=sender_email,
        to_emails=email,
        subject='Verify your Finzeed account',
        html_content=html_content
    )

    try:
        sg = SendGridAPIClient(sendgrid_key)
        response = sg.send(message)
        logging.info(f"Verification email sent to {email}, status: {response.status_code}")
        return response.status_code in (200, 201, 202)
    except Exception as e:
        logging.error(f"SendGrid error: {str(e)}")
        return False

# ========== AUTH HANDLERS ==========

def handle_register(req_body):
    """Handle user registration with email verification"""
    email = req_body.get('email', '').strip().lower()
    password = req_body.get('password', '')
    firstname = req_body.get('firstname', '').strip()
    lastname = req_body.get('lastname', '').strip()
    company = req_body.get('company', '').strip()
    mobile = req_body.get('mobile', '').strip()

    if not email or not password or not firstname or not lastname:
        return make_response({"error": "Email, password, first name, and last name are required"}, 400)

    if len(password) < 8:
        return make_response({"error": "Password must be at least 8 characters"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_auth_columns(cursor)

        # Check if email already exists with a password
        cursor.execute(
            "SELECT id, password_hash FROM users WHERE email = ?",
            (email,)
        )
        existing = cursor.fetchone()

        pw_hash, pw_salt = hash_password(password)
        verification_token = secrets.token_urlsafe(48)

        if existing:
            if existing[1]:  # already has password_hash
                cursor.close()
                conn.close()
                return make_response({"error": "An account with this email already exists. Please sign in."}, 409)
            # Upgrade anonymous user
            user_id = existing[0]
            cursor.execute("""
                UPDATE users SET password_hash = ?, password_salt = ?,
                    firstname = ?, lastname = ?, company_name = ?, mobile = ?,
                    email_verified = 0, verification_token = ?, updated_at = GETDATE()
                WHERE id = ?
            """, (pw_hash, pw_salt, firstname, lastname, company, mobile, verification_token, user_id))
        else:
            cursor.execute("""
                INSERT INTO users (email, password_hash, password_salt, firstname, lastname, company_name, mobile,
                    email_verified, verification_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """, (email, pw_hash, pw_salt, firstname, lastname, company, mobile, verification_token))
            cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
            user_id = cursor.fetchone()[0]

        conn.commit()
        cursor.close()
        conn.close()

        # Send verification email — if SendGrid not configured, auto-verify
        email_sent = send_verification_email(email, verification_token, firstname)

        if not email_sent:
            # Auto-verify if email can't be sent
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute("UPDATE users SET email_verified = 1, verification_token = NULL WHERE id = ?", (user_id,))
            conn2.commit()
            cur2.close()
            conn2.close()
            logging.info(f"Auto-verified {email} (SendGrid not configured)")

        return make_response({
            "success": True,
            "email_verification_required": email_sent,
            "email_sent": email_sent,
            "message": "Account created!" if not email_sent else "Account created! Please check your email to verify your account before signing in."
        }, 201)

    except pyodbc.OperationalError as e:
        logging.error(f"Registration DB error: {str(e)}")
        return make_response({"error": "Service temporarily unavailable. Please wait a moment and try again."}, 503)
    except Exception as e:
        logging.error(f"Registration error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return make_response({"error": "Registration failed. Please try again."}, 500)

def handle_login(req_body):
    """Handle user login — blocks unverified users"""
    email = req_body.get('email', '').strip().lower()
    password = req_body.get('password', '')

    if not email or not password:
        return make_response({"error": "Email and password are required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, email, password_hash, password_salt, firstname, lastname, company_name, mobile,
                   email_verified, verification_token
            FROM users WHERE email = ?
        """, (email,))
        row = cursor.fetchone()

        if not row or not row[2]:  # no user or no password set
            cursor.close()
            conn.close()
            return make_response({"error": "Invalid email or password"}, 401)

        user_id, user_email, stored_hash, stored_salt = row[0], row[1], row[2], row[3]
        check_hash, _ = hash_password(password, stored_salt)

        if check_hash != stored_hash:
            cursor.close()
            conn.close()
            return make_response({"error": "Invalid email or password"}, 401)

        # Check email verification
        email_verified = row[8]
        if not email_verified:
            cursor.close()
            conn.close()
            return make_response({
                "error": "Please verify your email before signing in. Check your inbox for the verification link.",
                "email_not_verified": True,
                "email": email
            }, 403)

        cursor.close()
        conn.close()

        token = create_jwt_token(user_id, user_email)

        return make_response({
            "success": True,
            "token": token,
            "user": {
                "id": user_id,
                "email": user_email,
                "firstname": row[4] or '',
                "lastname": row[5] or '',
                "company_name": row[6] or '',
                "mobile": row[7] or ''
            }
        })

    except Exception as e:
        logging.error(f"Login error: {str(e)}")
        return make_response({"error": "Login failed. Please try again."}, 500)

def handle_verify_email(req_body):
    """Verify user email with token"""
    token = req_body.get('token', '').strip()

    if not token:
        return make_response({"error": "Verification token is required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, email, firstname FROM users WHERE verification_token = ?",
            (token,)
        )
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return make_response({"error": "Invalid or expired verification link"}, 400)

        cursor.execute("""
            UPDATE users SET email_verified = 1, verification_token = NULL, updated_at = GETDATE()
            WHERE id = ?
        """, (row[0],))

        conn.commit()
        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "message": "Email verified successfully! You can now sign in."
        })

    except Exception as e:
        logging.error(f"Email verification error: {str(e)}")
        return make_response({"error": "Verification failed. Please try again."}, 500)

def handle_resend_verification(req_body):
    """Resend verification email"""
    email = req_body.get('email', '').strip().lower()

    if not email:
        return make_response({"error": "Email is required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, firstname, email_verified, verification_token FROM users WHERE email = ?",
            (email,)
        )
        row = cursor.fetchone()

        if not row:
            # Don't reveal whether email exists
            return make_response({"success": True, "message": "If this email is registered, a verification link has been sent."})

        if row[2]:  # already verified
            return make_response({"success": True, "message": "This email is already verified. You can sign in."})

        # Generate new token if needed
        verification_token = row[3] or secrets.token_urlsafe(48)
        if not row[3]:
            cursor.execute(
                "UPDATE users SET verification_token = ? WHERE id = ?",
                (verification_token, row[0])
            )
            conn.commit()

        cursor.close()
        conn.close()

        send_verification_email(email, verification_token, row[1] or 'there')

        return make_response({"success": True, "message": "Verification email sent. Please check your inbox."})

    except Exception as e:
        logging.error(f"Resend verification error: {str(e)}")
        return make_response({"error": "Could not send verification email. Please try again."}, 500)

def handle_profile(req, req_body):
    """Get user profile and application history"""
    payload = verify_jwt_token(req)
    if not payload:
        return make_response({"error": "Please sign in to view your profile"}, 401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get user info
        cursor.execute("""
            SELECT id, email, firstname, lastname, company_name, mobile
            FROM users WHERE id = ?
        """, (payload['user_id'],))
        user_row = cursor.fetchone()

        if not user_row:
            cursor.close()
            conn.close()
            return make_response({"error": "User not found"}, 404)

        user = {
            "id": user_row[0],
            "email": user_row[1],
            "firstname": user_row[2] or '',
            "lastname": user_row[3] or '',
            "company_name": user_row[4] or '',
            "mobile": user_row[5] or ''
        }

        # Get application history — show customer-facing status, not AI decision
        applications = []
        try:
            cursor.execute("""
                SELECT a.id, a.company_name, a.ai_assessed_at,
                       COALESCE(a.application_status, 'submitted') as app_status,
                       ad.decision as final_decision,
                       ad.final_credit_limit, ad.final_tenor_months,
                       a.customer_notified,
                       ad.internal_notes as decision_notes
                FROM applications a
                LEFT JOIN application_decisions ad ON a.id = ad.application_id
                WHERE a.user_id = ?
                ORDER BY a.ai_assessed_at DESC
            """, (payload['user_id'],))
            for app_row in cursor.fetchall():
                app_status = app_row[3] or 'submitted'
                final_decision = app_row[4]
                notified = app_row[7]
                decision_notes = app_row[8] or ''

                # Customer sees friendly status
                if final_decision == 'APPROVED':
                    display_status = 'Approved'
                    display_credit = float(app_row[5]) if app_row[5] else 0
                    display_tenor = app_row[6] or 0
                elif final_decision == 'REJECTED':
                    display_status = 'Rejected'
                    display_credit = 0
                    display_tenor = 0
                elif final_decision == 'REQUEST_INFO':
                    display_status = 'Info Requested'
                    display_credit = 0
                    display_tenor = 0
                elif app_status in ('ai_reviewed', 'pending_review'):
                    display_status = 'Under Review'
                    display_credit = 0
                    display_tenor = 0
                else:
                    display_status = 'Submitted'
                    display_credit = 0
                    display_tenor = 0

                applications.append({
                    "id": app_row[0],
                    "company_name": app_row[1] or '',
                    "status": display_status,
                    "credit_limit": display_credit,
                    "tenor_months": display_tenor,
                    "assessed_at": str(app_row[2]) if app_row[2] else '',
                    "notes": decision_notes if final_decision == 'REQUEST_INFO' else ''
                })
        except Exception as e:
            logging.warning(f"Could not fetch applications: {str(e)}")

        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "user": user,
            "applications": applications
        })

    except Exception as e:
        logging.error(f"Profile error: {str(e)}")
        return make_response({"error": "Could not load profile"}, 500)

def handle_update_profile(req, req_body):
    """Update user profile"""
    payload = verify_jwt_token(req)
    if not payload:
        return make_response({"error": "Please sign in to update your profile"}, 401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        firstname = req_body.get('firstname', '').strip()
        lastname = req_body.get('lastname', '').strip()
        company = req_body.get('company', '').strip()
        mobile = req_body.get('mobile', '').strip()

        cursor.execute("""
            UPDATE users SET firstname = ?, lastname = ?, company_name = ?, mobile = ?, updated_at = GETDATE()
            WHERE id = ?
        """, (firstname, lastname, company, mobile, payload['user_id']))

        conn.commit()
        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "user": {
                "id": payload['user_id'],
                "email": payload['email'],
                "firstname": firstname,
                "lastname": lastname,
                "company_name": company,
                "mobile": mobile
            }
        })

    except Exception as e:
        logging.error(f"Profile update error: {str(e)}")
        return make_response({"error": "Could not update profile"}, 500)

# ========== BACKOFFICE HANDLERS ==========

def verify_backoffice_token(req):
    """Verify JWT for backoffice users — checks backoffice_users table"""
    if not jwt:
        return None
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:]
    try:
        secret = os.environ.get('JWT_SECRET')
        if not secret:
            return None
        payload = jwt.decode(token, secret, algorithms=['HS256'])
        if payload.get('user_type') != 'backoffice':
            return None
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def handle_backoffice_login(req_body):
    """Handle backoffice user login"""
    email = req_body.get('email', '').strip().lower()
    password = req_body.get('password', '')

    if not email or not password:
        return make_response({"error": "Email and password are required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_new_tables(cursor)

        cursor.execute("""
            SELECT id, email, password_hash, password_salt, fullname, role, is_active
            FROM backoffice_users WHERE email = ?
        """, (email,))
        row = cursor.fetchone()

        if not row or not row[2]:
            cursor.close()
            conn.close()
            return make_response({"error": "Invalid email or password"}, 401)

        if not row[6]:
            cursor.close()
            conn.close()
            return make_response({"error": "Account deactivated"}, 403)

        stored_hash, stored_salt = row[2], row[3]
        check_hash, _ = hash_password(password, stored_salt)

        if check_hash != stored_hash:
            cursor.close()
            conn.close()
            return make_response({"error": "Invalid email or password"}, 401)

        # Update last login
        cursor.execute("UPDATE backoffice_users SET last_login = GETDATE() WHERE id = ?", (row[0],))
        conn.commit()
        cursor.close()
        conn.close()

        # Create JWT with backoffice marker
        secret = os.environ.get('JWT_SECRET')
        token = jwt.encode({
            'user_id': row[0],
            'email': row[1],
            'user_type': 'backoffice',
            'role': row[5] or 'analyst',
            'exp': datetime.now(timezone.utc) + timedelta(days=1),
            'iat': datetime.now(timezone.utc)
        }, secret, algorithm='HS256')

        log_audit(row[0], 'backoffice', 'login', 'backoffice_users', row[0])

        return make_response({
            "success": True,
            "token": token,
            "user": {
                "id": row[0],
                "email": row[1],
                "fullname": row[4] or '',
                "role": row[5] or 'analyst'
            }
        })

    except Exception as e:
        logging.error(f"Backoffice login error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return make_response({"error": f"Login failed: {str(e)}"}, 500)


def handle_seed_admin(req_body):
    """One-time endpoint to create the first backoffice admin user.
    Protected by a setup_key that must match ANTHROPIC_API_KEY (or a dedicated SETUP_KEY)."""
    setup_key = req_body.get('setup_key', '')
    expected_key = os.environ.get('SETUP_KEY') or os.environ.get('ANTHROPIC_API_KEY', '')

    if not setup_key or setup_key != expected_key:
        return make_response({"error": "Invalid setup key"}, 403)

    email = req_body.get('email', '').strip().lower()
    password = req_body.get('password', '')
    fullname = req_body.get('fullname', '').strip()

    if not email or not password or len(password) < 8:
        return make_response({"error": "Email and password (8+ chars) required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_new_tables(cursor)

        # Check if admin already exists
        cursor.execute("SELECT id FROM backoffice_users WHERE email = ?", (email,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return make_response({"error": "Admin user already exists"}, 409)

        pw_hash, pw_salt = hash_password(password)
        cursor.execute("""
            INSERT INTO backoffice_users (email, password_hash, password_salt, fullname, role, is_active)
            VALUES (?, ?, ?, ?, 'admin', 1)
        """, (email, pw_hash, pw_salt, fullname or 'Admin'))
        conn.commit()
        cursor.close()
        conn.close()

        logging.info(f"Backoffice admin created: {email}")
        return make_response({"success": True, "message": f"Admin user {email} created successfully"}, 201)

    except Exception as e:
        logging.error(f"Seed admin error: {str(e)}")
        return make_response({"error": "Failed to create admin"}, 500)


def handle_backoffice_dashboard(req, req_body):
    """Get backoffice dashboard statistics"""
    import time as _time
    payload = verify_backoffice_token(req)
    if not payload:
        return make_response({"error": "Unauthorized"}, 401)

    try:
        t0 = _time.time()
        conn = get_db_connection()
        t1 = _time.time()
        logging.info(f"PERF: DB connection took {t1-t0:.3f}s")
        cursor = conn.cursor()

        stats = {}

        # Total applications
        cursor.execute("SELECT COUNT(*) FROM applications")
        stats['total_applications'] = cursor.fetchone()[0]

        # By status
        cursor.execute("""
            SELECT COALESCE(application_status, 'submitted'), COUNT(*)
            FROM applications GROUP BY application_status
        """)
        status_counts = {}
        for row in cursor.fetchall():
            status_counts[row[0] or 'submitted'] = row[1]
        stats['by_status'] = status_counts

        # Today's applications
        cursor.execute("SELECT COUNT(*) FROM applications WHERE CAST(ai_assessed_at AS DATE) = CAST(GETDATE() AS DATE)")
        stats['today'] = cursor.fetchone()[0]

        # This week
        cursor.execute("SELECT COUNT(*) FROM applications WHERE ai_assessed_at >= DATEADD(day, -7, GETDATE())")
        stats['this_week'] = cursor.fetchone()[0]

        # Total approved credit
        cursor.execute("SELECT COALESCE(SUM(final_credit_limit), 0) FROM application_decisions WHERE decision = 'APPROVED'")
        stats['total_credit_approved'] = float(cursor.fetchone()[0])

        # Recent applications
        cursor.execute("""
            SELECT TOP 10 a.id, a.company_name, a.email, a.annual_revenue,
                   COALESCE(a.application_status, 'submitted'), a.ai_assessed_at,
                   cr.ai_recommendation, cr.confidence_score
            FROM applications a
            LEFT JOIN credit_reports cr ON a.id = cr.application_id
            ORDER BY a.ai_assessed_at DESC
        """)
        recent = []
        for row in cursor.fetchall():
            recent.append({
                "id": row[0],
                "company_name": row[1] or '',
                "email": row[2] or '',
                "annual_revenue": float(row[3]) if row[3] else 0,
                "status": row[4] or 'submitted',
                "assessed_at": str(row[5]) if row[5] else '',
                "ai_recommendation": row[6] or '',
                "confidence_score": float(row[7]) if row[7] else 0
            })
        stats['recent_applications'] = recent

        t2 = _time.time()
        logging.info(f"PERF: Dashboard queries took {t2-t1:.3f}s, total {t2-t0:.3f}s")
        cursor.close()
        conn.close()

        return make_response({"success": True, "stats": stats})

    except Exception as e:
        logging.error(f"Dashboard error: {str(e)}")
        return make_response({"error": "Could not load dashboard"}, 500)


def handle_backoffice_applications(req, req_body):
    """List all applications for backoffice with filters"""
    payload = verify_backoffice_token(req)
    if not payload:
        return make_response({"error": "Unauthorized"}, 401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        status_filter = req_body.get('status_filter')
        search = req_body.get('search', '').strip()
        page = int(req_body.get('page', 1))
        per_page = int(req_body.get('per_page', 20))
        offset = (page - 1) * per_page

        where_clauses = []
        params = []

        if status_filter:
            where_clauses.append("COALESCE(a.application_status, 'submitted') = ?")
            params.append(status_filter)

        if search:
            where_clauses.append("(a.company_name LIKE ? OR a.email LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%'])

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Count total
        cursor.execute(f"SELECT COUNT(*) FROM applications a {where_sql}", params)
        total = cursor.fetchone()[0]

        # Get applications
        cursor.execute(f"""
            SELECT a.id, a.company_name, a.firstname, a.lastname, a.email, a.mobile,
                   a.industry, a.annual_revenue, COALESCE(a.application_status, 'submitted'),
                   a.ai_assessed_at, a.ai_decision, a.ai_credit_limit, a.ai_confidence_score,
                   cr.ai_recommendation, cr.confidence_score as claude_confidence,
                   cr.executive_summary,
                   ad.decision as final_decision, ad.decided_at
            FROM applications a
            LEFT JOIN credit_reports cr ON a.id = cr.application_id
            LEFT JOIN application_decisions ad ON a.id = ad.application_id
            {where_sql}
            ORDER BY a.ai_assessed_at DESC
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
        """, params + [offset, per_page])

        applications = []
        for row in cursor.fetchall():
            applications.append({
                "id": row[0],
                "company_name": row[1] or '',
                "firstname": row[2] or '',
                "lastname": row[3] or '',
                "email": row[4] or '',
                "mobile": row[5] or '',
                "industry": row[6] or '',
                "annual_revenue": float(row[7]) if row[7] else 0,
                "status": row[8] or 'submitted',
                "assessed_at": str(row[9]) if row[9] else '',
                "ai_decision": row[10] or '',
                "ai_credit_limit": float(row[11]) if row[11] else 0,
                "ai_confidence": float(row[12]) if row[12] else 0,
                "claude_recommendation": row[13] or '',
                "claude_confidence": float(row[14]) if row[14] else 0,
                "executive_summary": row[15] or '',
                "final_decision": row[16] or '',
                "decided_at": str(row[17]) if row[17] else ''
            })

        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "applications": applications,
            "total": total,
            "page": page,
            "per_page": per_page
        })

    except Exception as e:
        logging.error(f"Applications list error: {str(e)}")
        return make_response({"error": "Could not load applications"}, 500)


def handle_backoffice_application_detail(req, req_body):
    """Get full application detail with AI report for backoffice"""
    payload = verify_backoffice_token(req)
    if not payload:
        return make_response({"error": "Unauthorized"}, 401)

    app_id = req_body.get('application_id')
    if not app_id:
        return make_response({"error": "application_id required"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Application details
        cursor.execute("""
            SELECT id, user_id, company_name, firstname, lastname, email, mobile,
                   industry, annual_revenue, purpose, status, application_status,
                   ai_decision, ai_credit_limit, ai_tenor_months, ai_interest_rate,
                   ai_confidence_score, ai_risk_factors, ai_recommendations, ai_assessed_at,
                   requested_amount, requested_installments, years_in_business,
                   business_description, declared_monthly_inflows
            FROM applications WHERE id = ?
        """, (app_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return make_response({"error": "Application not found"}, 404)

        application = {
            "id": row[0], "user_id": row[1], "company_name": row[2] or '',
            "firstname": row[3] or '', "lastname": row[4] or '',
            "email": row[5] or '', "mobile": row[6] or '',
            "industry": row[7] or '', "annual_revenue": float(row[8]) if row[8] else 0,
            "purpose": row[9] or '', "status": row[10] or '',
            "application_status": row[11] or 'submitted',
            "ai_decision": row[12] or '', "ai_credit_limit": float(row[13]) if row[13] else 0,
            "ai_tenor_months": row[14] or 0, "ai_interest_rate": float(row[15]) if row[15] else 0,
            "ai_confidence_score": float(row[16]) if row[16] else 0,
            "ai_risk_factors": json.loads(row[17]) if row[17] else [],
            "ai_recommendations": json.loads(row[18]) if row[18] else [],
            "assessed_at": str(row[19]) if row[19] else '',
            "requested_amount": float(row[20]) if row[20] else 0,
            "requested_installments": row[21] or 0,
            "years_in_business": row[22] or 0,
            "business_description": row[23] or '',
            "declared_monthly_inflows": float(row[24]) if row[24] else 0
        }

        # Claude credit report
        cursor.execute("""
            SELECT ai_recommendation, confidence_score, recommended_limit, recommended_tenor,
                   revenue_analysis, bank_analysis, balance_sheet_analysis, iscore_analysis,
                   identity_verification, risk_factors, positive_factors, executive_summary,
                   full_report, created_at
            FROM credit_reports WHERE application_id = ?
            ORDER BY created_at DESC
        """, (app_id,))
        cr_row = cursor.fetchone()

        claude_report = None
        if cr_row:
            claude_report = {
                "recommendation": cr_row[0] or '',
                "confidence_score": float(cr_row[1]) if cr_row[1] else 0,
                "recommended_limit": float(cr_row[2]) if cr_row[2] else 0,
                "recommended_tenor": cr_row[3] or 0,
                "revenue_analysis": json.loads(cr_row[4]) if cr_row[4] else {},
                "bank_analysis": json.loads(cr_row[5]) if cr_row[5] else {},
                "balance_sheet_analysis": json.loads(cr_row[6]) if cr_row[6] else {},
                "iscore_analysis": json.loads(cr_row[7]) if cr_row[7] else {},
                "identity_verification": json.loads(cr_row[8]) if cr_row[8] else {},
                "risk_factors": json.loads(cr_row[9]) if cr_row[9] else [],
                "positive_factors": json.loads(cr_row[10]) if cr_row[10] else [],
                "executive_summary": cr_row[11] or '',
                "full_report": json.loads(cr_row[12]) if cr_row[12] else {},
                "created_at": str(cr_row[13]) if cr_row[13] else ''
            }

        # Documents
        cursor.execute("""
            SELECT id, document_type, filename, blob_url, file_size
            FROM documents WHERE application_id = ?
        """, (app_id,))
        documents = []
        for doc_row in cursor.fetchall():
            documents.append({
                "id": doc_row[0], "type": doc_row[1] or '',
                "filename": doc_row[2] or '', "blob_url": doc_row[3] or '',
                "file_size": doc_row[4] or 0
            })

        # Previous decisions
        cursor.execute("""
            SELECT ad.id, ad.decision, ad.final_credit_limit, ad.final_tenor_months,
                   ad.final_interest_rate, ad.override_reason, ad.internal_notes,
                   ad.customer_notified, ad.decided_at, bu.fullname
            FROM application_decisions ad
            LEFT JOIN backoffice_users bu ON ad.decided_by = bu.id
            WHERE ad.application_id = ?
            ORDER BY ad.decided_at DESC
        """, (app_id,))
        decisions = []
        for d_row in cursor.fetchall():
            decisions.append({
                "id": d_row[0], "decision": d_row[1] or '',
                "final_credit_limit": float(d_row[2]) if d_row[2] else 0,
                "final_tenor_months": d_row[3] or 0,
                "final_interest_rate": float(d_row[4]) if d_row[4] else 0,
                "override_reason": d_row[5] or '', "internal_notes": d_row[6] or '',
                "customer_notified": bool(d_row[7]),
                "decided_at": str(d_row[8]) if d_row[8] else '',
                "decided_by": d_row[9] or ''
            })

        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "application": application,
            "claude_report": claude_report,
            "documents": documents,
            "decisions": decisions
        })

    except Exception as e:
        logging.error(f"Application detail error: {str(e)}")
        return make_response({"error": "Could not load application"}, 500)


def handle_backoffice_decide(req, req_body):
    """Approve/Reject an application from backoffice"""
    payload = verify_backoffice_token(req)
    if not payload:
        return make_response({"error": "Unauthorized"}, 401)

    if payload.get('role') == 'viewer':
        return make_response({"error": "Viewers cannot make decisions"}, 403)

    app_id = req_body.get('application_id')
    decision = req_body.get('decision')  # APPROVED, REJECTED, REQUEST_INFO
    if not app_id or not decision:
        return make_response({"error": "application_id and decision required"}, 400)

    if decision not in ('APPROVED', 'REJECTED', 'REQUEST_INFO'):
        return make_response({"error": "Invalid decision"}, 400)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Insert decision
        cursor.execute("""
            INSERT INTO application_decisions (
                application_id, decided_by, decision, final_credit_limit,
                final_tenor_months, final_interest_rate, override_reason,
                internal_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            app_id, payload['user_id'], decision,
            req_body.get('final_credit_limit'),
            req_body.get('final_tenor_months'),
            req_body.get('final_interest_rate', 42.0),
            req_body.get('override_reason', ''),
            req_body.get('internal_notes', '')
        ))

        # Update application status
        new_status = 'approved' if decision == 'APPROVED' else 'rejected' if decision == 'REJECTED' else 'info_requested'
        cursor.execute("""
            UPDATE applications SET application_status = ? WHERE id = ?
        """, (new_status, app_id))

        conn.commit()

        # Audit log
        log_audit(payload['user_id'], 'backoffice', f'decision_{decision.lower()}',
                  'application', app_id, json.dumps({
                      "decision": decision,
                      "credit_limit": req_body.get('final_credit_limit'),
                      "override_reason": req_body.get('override_reason', '')
                  }))

        cursor.close()
        conn.close()

        return make_response({
            "success": True,
            "message": f"Application {decision.lower()} successfully",
            "status": new_status
        })

    except Exception as e:
        logging.error(f"Decision error: {str(e)}")
        return make_response({"error": "Could not save decision"}, 500)


def handle_backoffice_suppliers(req, req_body):
    """CRUD operations for suppliers (backoffice)"""
    payload = verify_backoffice_token(req)
    if not payload:
        return make_response({"error": "Unauthorized"}, 401)

    operation = req_body.get('operation', 'list')

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_new_tables(cursor)

        if operation == 'list':
            cursor.execute("""
                SELECT id, name, category, location, invoice_range_min, invoice_range_max,
                       payment_terms, status, is_verified, created_at
                FROM suppliers ORDER BY name
            """)
            suppliers = []
            for row in cursor.fetchall():
                suppliers.append({
                    "id": row[0], "name": row[1] or '', "category": row[2] or '',
                    "location": row[3] or '',
                    "invoice_range_min": float(row[4]) if row[4] else 0,
                    "invoice_range_max": float(row[5]) if row[5] else 0,
                    "payment_terms": row[6] or '', "status": row[7] or 'Active',
                    "is_verified": bool(row[8]),
                    "created_at": str(row[9]) if row[9] else ''
                })
            cursor.close()
            conn.close()
            return make_response({"success": True, "suppliers": suppliers})

        elif operation == 'create':
            if payload.get('role') == 'viewer':
                return make_response({"error": "Insufficient permissions"}, 403)
            cursor.execute("""
                INSERT INTO suppliers (name, category, location, invoice_range_min,
                    invoice_range_max, payment_terms, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                req_body.get('name'), req_body.get('category'),
                req_body.get('location'), req_body.get('invoice_range_min'),
                req_body.get('invoice_range_max'), req_body.get('payment_terms'),
                req_body.get('status', 'Active')
            ))
            conn.commit()
            cursor.close()
            conn.close()
            return make_response({"success": True, "message": "Supplier created"}, 201)

        elif operation == 'update':
            if payload.get('role') == 'viewer':
                return make_response({"error": "Insufficient permissions"}, 403)
            supplier_id = req_body.get('supplier_id')
            cursor.execute("""
                UPDATE suppliers SET name = ?, category = ?, location = ?,
                    invoice_range_min = ?, invoice_range_max = ?,
                    payment_terms = ?, status = ?, updated_at = GETDATE()
                WHERE id = ?
            """, (
                req_body.get('name'), req_body.get('category'),
                req_body.get('location'), req_body.get('invoice_range_min'),
                req_body.get('invoice_range_max'), req_body.get('payment_terms'),
                req_body.get('status', 'Active'), supplier_id
            ))
            conn.commit()
            cursor.close()
            conn.close()
            return make_response({"success": True, "message": "Supplier updated"})

        elif operation == 'delete':
            if payload.get('role') != 'admin':
                return make_response({"error": "Only admins can delete suppliers"}, 403)
            cursor.execute("DELETE FROM suppliers WHERE id = ?", (req_body.get('supplier_id'),))
            conn.commit()
            cursor.close()
            conn.close()
            return make_response({"success": True, "message": "Supplier deleted"})

        cursor.close()
        conn.close()
        return make_response({"error": "Invalid operation"}, 400)

    except Exception as e:
        logging.error(f"Supplier error: {str(e)}")
        return make_response({"error": "Supplier operation failed"}, 500)


def handle_contact_us(req_body):
    """Handle contact us form submissions"""
    name = req_body.get('name', '').strip()
    email = req_body.get('email', '').strip()
    subject = req_body.get('subject', '').strip()
    message = req_body.get('message', '').strip()

    if not name or not email or not message:
        return make_response({"error": "Name, email, and message are required"}, 400)

    logging.info(f"Contact form submission from {name} ({email}): {subject}")

    # Try to send via SendGrid
    sendgrid_key = os.environ.get('SENDGRID_API_KEY')
    contact_email = os.environ.get('CONTACT_EMAIL', 'contact@finzeed.com')

    if sendgrid_key and SendGridAPIClient:
        try:
            import html as html_module
            safe_name = html_module.escape(name)
            safe_subject = html_module.escape(subject or 'Contact Form')
            safe_message = html_module.escape(message)

            mail = Mail(
                from_email=os.environ.get('SENDGRID_SENDER_EMAIL', 'noreply@finzeed.com'),
                to_emails=contact_email,
                subject=f'Finzeed Contact: {safe_subject}',
                html_content=f"""
                <h3>New Contact Form Submission</h3>
                <p><strong>Name:</strong> {safe_name}</p>
                <p><strong>Email:</strong> {email}</p>
                <p><strong>Subject:</strong> {safe_subject}</p>
                <p><strong>Message:</strong><br>{safe_message}</p>
                """
            )
            sg = SendGridAPIClient(sendgrid_key)
            sg.send(mail)
        except Exception as e:
            logging.warning(f"Contact email failed: {str(e)}")

    return make_response({
        "success": True,
        "message": "Thank you for contacting us! We'll get back to you within 24 hours."
    })


# ========== BACKOFFICE UPLOAD HANDLER ==========

def handle_backoffice_upload(req):
    """Handle document upload by backoffice employees on behalf of a customer.
    Accepts files + customer info, runs AI analysis, saves to DB."""
    # Verify backoffice JWT
    payload = verify_backoffice_token(req)
    if not payload:
        return func.HttpResponse(
            json.dumps({"error": "Unauthorized"}), status_code=401,
            mimetype="application/json", headers={'Access-Control-Allow-Origin': '*'})

    try:
        # Extract form fields
        company = req.form.get('company', '')
        firstname = req.form.get('firstname', '')
        lastname = req.form.get('lastname', '')
        email = req.form.get('email', '')
        mobile = req.form.get('mobile', '')
        revenue = req.form.get('revenue', '0')
        industry = req.form.get('industry', '')
        purpose = req.form.get('purpose', '')
        years_in_business = req.form.get('years_in_business', '')
        business_description = req.form.get('business_description', '')
        requested_amount = req.form.get('requested_amount', '')
        requested_installments = req.form.get('requested_installments', '')
        declared_monthly_inflows = req.form.get('declared_monthly_inflows', '0')
        application_id = req.form.get('application_id', '')  # optional: attach to existing app

        try:
            revenue = float(revenue.replace(',', '')) if revenue else 0
        except:
            revenue = 0

        logging.info(f"Backoffice upload by {payload['email']} for customer {email or company}")

        # Get all uploaded files
        bank_statements = req.files.getlist('bank_statements') or req.files.getlist('bankStatements') or []
        national_id_files = req.files.getlist('national_id') or req.files.getlist('nationalId') or []
        iscore_files = req.files.getlist('iscore') or req.files.getlist('iScore') or []
        balance_sheet_files = req.files.getlist('balance_sheet') or req.files.getlist('balanceSheet') or []
        commercial_reg_files = req.files.getlist('commercial_registration') or req.files.getlist('commercialRegistration') or []
        tax_card_files = req.files.getlist('tax_card') or req.files.getlist('taxCard') or []

        logging.info(f"Backoffice files: {len(bank_statements)} bank, {len(national_id_files)} ID, "
                     f"{len(iscore_files)} iScore, {len(balance_sheet_files)} balance sheet")

        # Build data structure
        data = {
            'company': company, 'firstname': firstname, 'lastname': lastname,
            'email': email, 'mobile': mobile, 'revenue': revenue,
            'industry': industry, 'purpose': purpose,
            'years_in_business': years_in_business,
            'business_description': business_description,
            'requested_amount': requested_amount,
            'requested_installments': requested_installments,
            'declared_monthly_inflows': declared_monthly_inflows,
            'documents': {
                'bank_statements': [], 'national_id': [], 'iscore': [],
                'balance_sheet': [], 'commercial_registration': [], 'tax_card': []
            }
        }

        # Process bank statements
        for bs in bank_statements:
            file_content = bs.read()
            data['documents']['bank_statements'].append({
                'filename': bs.filename,
                'content': base64.b64encode(file_content).decode('utf-8'),
                'size': len(file_content)
            })

        # Process other document types
        for doc_type, file_list in [
            ('national_id', national_id_files), ('iscore', iscore_files),
            ('balance_sheet', balance_sheet_files),
            ('commercial_registration', commercial_reg_files),
            ('tax_card', tax_card_files)
        ]:
            for doc_file in file_list:
                file_content = doc_file.read()
                data['documents'][doc_type].append({
                    'filename': doc_file.filename,
                    'content': base64.b64encode(file_content).decode('utf-8'),
                    'size': len(file_content)
                })

        # Run AI assessment (OpenAI extraction + Claude analysis)
        ai_assessment = perform_ai_assessment(data)

        # Save to DB
        db_result = save_application_to_db(data, ai_assessment)
        app_id = db_result.get('application_id') or application_id

        # Save Claude report
        claude_report = ai_assessment.get('claude_report')
        if claude_report and app_id:
            save_credit_report(app_id, claude_report)

        # Audit log
        log_audit(payload['user_id'], 'backoffice', 'upload_documents', 'application', app_id,
                  json.dumps({"uploaded_by": payload['email'], "company": company}))

        # Return full results to backoffice (they see everything)
        response_data = {
            'success': True,
            'application_id': app_id,
            'ai_assessment': {
                'decision': ai_assessment.get('decision'),
                'credit_limit': ai_assessment.get('credit_limit'),
                'tenor_months': ai_assessment.get('tenor_months'),
                'confidence_score': ai_assessment.get('confidence_score'),
                'risk_factors': ai_assessment.get('risk_factors'),
                'bank_analysis': ai_assessment.get('bank_analysis')
            },
            'claude_report': claude_report,
            'message': f'Application created and AI analysis complete for {company or email}'
        }

        return func.HttpResponse(
            json.dumps(response_data, default=str), status_code=200,
            mimetype="application/json", headers={'Access-Control-Allow-Origin': '*'})

    except Exception as e:
        logging.error(f"Backoffice upload error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": f"Upload processing error: {str(e)}"}), status_code=500,
            mimetype="application/json", headers={'Access-Control-Allow-Origin': '*'})


# ========== EXISTING HANDLERS ==========

def save_application_to_db(data, ai_assessment):
    """Save application to database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Ensure new schema tables exist
        ensure_new_tables(cursor)

        # 1. Insert or update user
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM users WHERE email = ?)
                UPDATE users
                SET company_name = ?, firstname = ?, lastname = ?, mobile = ?, updated_at = GETDATE()
                WHERE email = ?
            ELSE
                INSERT INTO users (email, company_name, firstname, lastname, mobile)
                VALUES (?, ?, ?, ?, ?)
        """, (
            data['email'],
            data['company'], data['firstname'], data['lastname'], data['mobile'], data['email'],
            data['email'], data['company'], data['firstname'], data['lastname'], data['mobile']
        ))

        # Get user_id
        cursor.execute("SELECT id FROM users WHERE email = ?", (data['email'],))
        user_id = cursor.fetchone()[0]

        # Determine application_status based on Claude analysis
        claude_report = ai_assessment.get('claude_report')
        app_status = 'ai_reviewed' if claude_report else 'submitted'

        # Helper to convert empty strings to None for numeric fields
        def to_num(val):
            if val is None or val == '':
                return None
            try:
                return float(str(val).replace(',', ''))
            except (ValueError, TypeError):
                return None

        def to_int(val):
            if val is None or val == '':
                return None
            try:
                return int(float(str(val).replace(',', '')))
            except (ValueError, TypeError):
                return None

        # 2. Insert application
        cursor.execute("""
            INSERT INTO applications (
                user_id, company_name, firstname, lastname, email, mobile,
                industry, annual_revenue, purpose, status, application_status,
                ai_decision, ai_credit_limit, ai_tenor_months, ai_interest_rate,
                ai_confidence_score, ai_risk_factors, ai_recommendations, ai_assessed_at,
                requested_amount, requested_installments,
                years_in_business, business_description, declared_monthly_inflows
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE(), ?, ?, ?, ?, ?)
        """, (
            user_id, data['company'], data['firstname'], data['lastname'],
            data['email'], data['mobile'], data.get('industry'),
            to_num(data.get('revenue')), data.get('purpose'),
            'ai_assessment', app_status,
            ai_assessment.get('decision'),
            to_num(ai_assessment.get('credit_limit')),
            to_int(ai_assessment.get('tenor_months')),
            to_num(ai_assessment.get('interest_rate')),
            to_num(ai_assessment.get('confidence_score')),
            json.dumps(ai_assessment.get('risk_factors', [])),
            json.dumps(ai_assessment.get('recommendations', [])),
            to_num(data.get('requested_amount')),
            to_int(data.get('requested_installments')),
            to_int(data.get('years_in_business')),
            data.get('business_description') or None,
            to_num(data.get('declared_monthly_inflows'))
        ))
        
        # Get application_id
        cursor.execute("SELECT @@IDENTITY")
        application_id = cursor.fetchone()[0]
        
        # 3. Upload documents to blob storage and save references
        documents = data.get('documents', {})
        container_name = 'application-documents'
        try:
            blob_service_client.create_container(container_name)
        except Exception:
            pass  # Container likely already exists

        for doc_type, doc_list in documents.items():
            if not doc_list:
                continue
            # Handle both list and dict formats
            if isinstance(doc_list, dict):
                doc_list = [doc_list]
            if not isinstance(doc_list, list):
                continue

            for doc_info in doc_list:
                if not isinstance(doc_info, dict):
                    continue
                filename = doc_info.get('filename', '')
                if not filename:
                    continue

                # Upload to blob storage if content is available
                blob_url = doc_info.get('blob_url', '')
                file_size = doc_info.get('size', doc_info.get('file_size', 0))

                if not blob_url and doc_info.get('content'):
                    try:
                        blob_name = f"{application_id}/{doc_type}/{filename}"
                        blob_client = blob_service_client.get_blob_client(container_name, blob_name)
                        file_bytes = base64.b64decode(doc_info['content'])
                        blob_client.upload_blob(file_bytes, overwrite=True)
                        blob_url = blob_client.url
                        file_size = len(file_bytes)
                        logging.info(f"Uploaded {doc_type}/{filename} to blob storage")
                    except Exception as blob_err:
                        logging.warning(f"Blob upload failed for {filename}: {blob_err}")
                        blob_url = f"upload-pending://{doc_type}/{filename}"

                cursor.execute("""
                    INSERT INTO documents (application_id, document_type, filename, blob_url, file_size)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    application_id,
                    doc_type,
                    filename,
                    blob_url,
                    file_size
                ))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {
            'success': True,
            'user_id': user_id,
            'application_id': int(application_id)
        }
        
    except Exception as e:
        logging.error(f"Database error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e)
        }

@app.route(route="finzeed_ai_functions", methods=["GET", "POST", "OPTIONS"])
def finzeed_ai_functions(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Finzeed AI Functions endpoint called')
    
    # Handle CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse(
            status_code=200,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization'
            }
        )
    
    try:
        # Check if this is a multipart/form-data request (file upload)
        content_type = req.headers.get('Content-Type', '')
        
        if 'multipart/form-data' in content_type:
            # Check if this is a backoffice upload (has bo_upload field)
            if req.form.get('bo_upload') == '1':
                return handle_backoffice_upload(req)
            # This is a file upload request - handle differently
            return handle_form_data_request(req)
        else:
            # This is a JSON request (chat or old-style application)
            try:
                req_body = req.get_json()
            except ValueError:
                return func.HttpResponse(
                    json.dumps({"error": "Invalid JSON in request body"}),
                    status_code=400,
                    mimetype="application/json",
                    headers={'Access-Control-Allow-Origin': '*'}
                )
            
            # Check for auth actions
            action = req_body.get('action')
            if action == 'register':
                return handle_register(req_body)
            if action == 'login':
                return handle_login(req_body)
            if action == 'profile':
                return handle_profile(req, req_body)
            if action == 'update_profile':
                return handle_update_profile(req, req_body)
            if action == 'verify_email':
                return handle_verify_email(req_body)
            if action == 'resend_verification':
                return handle_resend_verification(req_body)

            # Backoffice actions
            if action == 'backoffice_login':
                return handle_backoffice_login(req_body)
            if action == 'seed_admin':
                return handle_seed_admin(req_body)
            if action == 'backoffice_applications':
                return handle_backoffice_applications(req, req_body)
            if action == 'backoffice_application_detail':
                return handle_backoffice_application_detail(req, req_body)
            if action == 'backoffice_decide':
                return handle_backoffice_decide(req, req_body)
            if action == 'backoffice_dashboard':
                return handle_backoffice_dashboard(req, req_body)
            if action == 'backoffice_suppliers':
                return handle_backoffice_suppliers(req, req_body)
            if action == 'contact_us':
                return handle_contact_us(req_body)

            # Check if it's a chat request
            if req_body.get('chat'):
                return handle_chat(req_body)

            # Otherwise, it's a credit application request
            return handle_credit_application(req_body)
        
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": "An internal error occurred. Please try again."}),
            status_code=500,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )

def handle_form_data_request(req):
    """Handle multipart/form-data requests with file uploads"""
    logging.info("Handling multipart/form-data request")
    
    try:
        # Extract form fields
        company = req.form.get('company', '')
        firstname = req.form.get('firstname', '')
        lastname = req.form.get('lastname', '')
        email = req.form.get('email', '')
        mobile = req.form.get('mobile', '')
        revenue = req.form.get('revenue', '0')
        industry = req.form.get('industry', '')
        purpose = req.form.get('purpose', '')
        years_in_business = req.form.get('years_in_business', '')
        business_description = req.form.get('business_description', '')
        requested_amount = req.form.get('requested_amount', '')
        requested_installments = req.form.get('requested_installments', '')
        declared_monthly_inflows = req.form.get('declared_monthly_inflows', '0')

        logging.info(f"Form data - Company: {company}, Email: {email}, Revenue: {revenue}")
        
        # Convert revenue to float
        try:
            revenue = float(revenue.replace(',', '')) if revenue else 0
        except:
            revenue = 0
        
        # Get uploaded files — all document types
        bank_statements = req.files.getlist('bank_statements') or req.files.getlist('bankStatements') or []
        national_id_files = req.files.getlist('national_id') or req.files.getlist('nationalId') or []
        iscore_files = req.files.getlist('iscore') or req.files.getlist('iScore') or []
        balance_sheet_files = req.files.getlist('balance_sheet') or req.files.getlist('balanceSheet') or []
        commercial_reg_files = req.files.getlist('commercial_registration') or req.files.getlist('commercialRegistration') or []
        tax_card_files = req.files.getlist('tax_card') or req.files.getlist('taxCard') or []

        logging.info(f"Received files: {len(bank_statements)} bank statements, "
                     f"{len(national_id_files)} national ID, {len(iscore_files)} i-Score, "
                     f"{len(balance_sheet_files)} balance sheet")

        # Prepare data structure
        data = {
            'company': company,
            'firstname': firstname,
            'lastname': lastname,
            'email': email,
            'mobile': mobile,
            'revenue': revenue,
            'industry': industry,
            'purpose': purpose,
            'years_in_business': years_in_business,
            'business_description': business_description,
            'requested_amount': requested_amount,
            'requested_installments': requested_installments,
            'declared_monthly_inflows': declared_monthly_inflows,
            'documents': {
                'bank_statements': [],
                'national_id': [],
                'iscore': [],
                'balance_sheet': [],
                'commercial_registration': [],
                'tax_card': []
            }
        }
        
        # Convert uploaded files to base64 for analysis
        for bank_statement in bank_statements:
            file_content = bank_statement.read()
            file_base64 = base64.b64encode(file_content).decode('utf-8')
            
            data['documents']['bank_statements'].append({
                'filename': bank_statement.filename,
                'content': file_base64,
                'size': len(file_content)
            })
            
            logging.info(f"Added bank statement: {bank_statement.filename} ({len(file_content)} bytes)")

        # Process additional document types (store for backoffice review)
        for doc_type, file_list in [
            ('national_id', national_id_files),
            ('iscore', iscore_files),
            ('balance_sheet', balance_sheet_files),
            ('commercial_registration', commercial_reg_files),
            ('tax_card', tax_card_files)
        ]:
            for doc_file in file_list:
                file_content = doc_file.read()
                file_base64 = base64.b64encode(file_content).decode('utf-8')
                data['documents'][doc_type].append({
                    'filename': doc_file.filename,
                    'content': file_base64,
                    'size': len(file_content)
                })
                logging.info(f"Added {doc_type}: {doc_file.filename} ({len(file_content)} bytes)")

        # Perform AI assessment with bank analysis (OpenAI extraction + Claude analysis)
        ai_assessment = perform_ai_assessment(data)

        # Save to database
        db_result = save_application_to_db(data, ai_assessment)
        logging.info(f"DB save result: {db_result}")

        # Save Claude credit report if available
        application_id = db_result.get('application_id')
        claude_report = ai_assessment.get('claude_report')
        if claude_report and application_id:
            try:
                save_credit_report(application_id, claude_report)
                log_audit(None, 'system', 'ai_analysis_complete', 'application', application_id,
                          json.dumps({"recommendation": claude_report.get('recommendation')}))
            except Exception as report_err:
                logging.error(f"Failed to save credit report: {report_err}")

        # NEW FLOW: Customer sees "Application Submitted" — NOT the AI decision
        # The AI decision is stored internally for the backoffice team
        response_data = {
            'status': 'submitted',
            'message': 'Your application has been submitted successfully! Our team will review it and get back to you within 24-48 hours.',
            'application_id': application_id,
            'application_status': 'submitted'
        }

        return func.HttpResponse(
            json.dumps(response_data),
            status_code=200,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )
        
    except Exception as e:
        logging.error(f"Form data handling error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return func.HttpResponse(
            json.dumps({"error": f"Document processing error: {str(e)}"}),
            status_code=500,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )

def handle_chat(req_body):
    """Handle chat requests with Azure OpenAI"""
    import requests
    
    message = req_body.get('message', '')
    history = req_body.get('history', [])
    
    # Azure OpenAI configuration
    api_key = os.environ.get('AZURE_OPENAI_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    deployment = os.environ.get('AZURE_OPENAI_DEPLOYMENT', 'finzeed-chat')
    
    if not api_key or not endpoint:
        return func.HttpResponse(
            json.dumps({"reply": "I'm having trouble connecting right now. Please try again later."}),
            status_code=200,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )
    
    # System prompt
    system_prompt = """You are a helpful assistant for Finzeed, an SME working capital financing platform in Egypt.

Key Information:
- Credit limits: EGP 250,000 to 5,000,000
- Interest rates: 3.5% per month (42% per annum)
- Tenor: 6 to 18 months
- We pay suppliers directly
- Customers repay in monthly instalments
- Fast approvals based on bank statement analysis

Be helpful, professional, and concise."""
    
    # Prepare messages
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add history (last 10 messages, only allow user/assistant roles)
    for msg in history[-10:]:
        role = msg.get('role', 'user')
        if role not in ('user', 'assistant'):
            role = 'user'
        messages.append({
            "role": role,
            "content": msg.get('content', '')
        })
    
    # Add current message
    messages.append({"role": "user", "content": message})
    
    # Call Azure OpenAI
    try:
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-08-01-preview"
        headers = {
            "Content-Type": "application/json",
            "api-key": api_key
        }
        payload = {
            "messages": messages,
            "max_tokens": 500,
            "temperature": 0.7
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            reply = result['choices'][0]['message']['content']
            
            return func.HttpResponse(
                json.dumps({"reply": reply}),
                status_code=200,
                mimetype="application/json",
                headers={'Access-Control-Allow-Origin': '*'}
            )
        else:
            logging.error(f"OpenAI API error: {response.status_code} - {response.text}")
            return func.HttpResponse(
                json.dumps({"reply": "I'm having trouble processing your request. Please try again."}),
                status_code=200,
                mimetype="application/json",
                headers={'Access-Control-Allow-Origin': '*'}
            )
            
    except Exception as e:
        logging.error(f"Chat error: {str(e)}")
        return func.HttpResponse(
            json.dumps({"reply": "Sorry, I encountered an error. Please try again."}),
            status_code=200,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )

def handle_credit_application(req_body):
    """Handle JSON credit application requests"""
    
    logging.info("Processing credit application (JSON format)")
    
    # Validate required fields
    required_fields = ['company', 'firstname', 'lastname', 'email', 'mobile', 'revenue']
    missing_fields = [field for field in required_fields if not req_body.get(field)]
    
    if missing_fields:
        return func.HttpResponse(
            json.dumps({"error": f"Missing required fields: {', '.join(missing_fields)}"}),
            status_code=400,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )
    
    try:
        # Perform AI assessment (dual-AI pipeline)
        ai_assessment = perform_ai_assessment(req_body)

        # Save to database
        db_result = save_application_to_db(req_body, ai_assessment)

        # Save Claude credit report if available
        application_id = db_result.get('application_id')
        claude_report = ai_assessment.get('claude_report')
        if claude_report and application_id:
            save_credit_report(application_id, claude_report)

        # Customer sees "submitted" — AI decision is internal only
        response_data = {
            'status': 'submitted',
            'message': 'Your application has been submitted successfully! Our team will review it and get back to you within 24-48 hours.',
            'application_id': application_id,
            'application_status': 'submitted'
        }

        return func.HttpResponse(
            json.dumps(response_data),
            status_code=200,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )
        
    except Exception as e:
        logging.error(f"Application processing error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        
        return func.HttpResponse(
            json.dumps({"error": f"Application processing error: {str(e)}"}),
            status_code=500,
            mimetype="application/json",
            headers={'Access-Control-Allow-Origin': '*'}
        )

def analyze_with_openai(text_content, filename):
    """
    Send bank statement text to Azure OpenAI for intelligent analysis
    
    AI extracts:
    - Total inflows (money coming IN)
    - Transaction count  
    - Time period
    
    Returns structured JSON
    """
    
    # Get Azure OpenAI credentials from environment
    api_key = os.environ.get('AZURE_OPENAI_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    deployment = os.environ.get('AZURE_OPENAI_DEPLOYMENT', 'finzeed-chat')
    
    if not api_key or not endpoint:
        logging.error("❌ Azure OpenAI credentials not configured")
        return None
    
    # Use full text — large bank statements can be 50K+ chars
    # GPT-4 supports 128K tokens, so we can send much more text
    max_text = 100000  # ~25K tokens, well within GPT-4 limits
    text_to_analyze = text_content[:max_text]
    text_truncated = len(text_content) > max_text
    truncation_note = f"\n\nNOTE: This text was truncated from {len(text_content)} to {max_text} characters. Extrapolate if needed." if text_truncated else ""

    logging.info(f"Sending {len(text_to_analyze)} chars to OpenAI (full text: {len(text_content)} chars)")

    # Prepare the prompt
    prompt = f"""You are a financial analyst specialized in analyzing bank statements for Egyptian SMEs.

TASK: Analyze this COMPLETE bank statement and extract the following information. This statement may cover multiple months — you MUST analyze ALL pages and ALL months.

BANK STATEMENT TEXT:
{text_to_analyze}
{truncation_note}

INSTRUCTIONS:
1. Extract TOTAL INFLOWS (money coming INTO the account) across ALL months in the statement:
   - Include: deposits, incoming transfers, customer payments, sales revenue, wire transfers IN, "IPN Inward", "Cash Deposit", "Account Transfer Collection", credit entries
   - Exclude: withdrawals, ATM, outgoing transfers, fees, purchases, money going OUT, "Outward", "POS Purchase", debit entries

2. Count the NUMBER of inflow transactions across ALL months

3. Determine the TIME PERIOD covered — count how many distinct months appear in the statement dates. A 12-month statement should report months=12.

IMPORTANT RULES:
- Analyze the ENTIRE statement from start to end — do NOT stop after the first page or first month
- Only count INFLOWS (credits/deposits/incoming money)
- Skip OUTFLOWS (debits/withdrawals/outgoing money)
- Ignore account balances (we only want transaction amounts)
- The statement may be in Arabic or English or mixed - handle both
- Look for transaction patterns and sum ALL inflow amounts across ALL pages and months
- If the statement covers January to December, months should be 12

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation, no code blocks):
{{
    "total_inflows": <total number in EGP>,
    "transaction_count": <number of inflow transactions>,
    "months": <number of months covered>,
    "currency": "EGP",
    "period": "<start month/year> to <end month/year>"
}}

If you cannot determine the information, return:
{{
    "total_inflows": 0,
    "transaction_count": 0,
    "months": 0,
    "currency": "EGP",
    "error": "Could not extract transaction data"
}}"""

    # Build API URL
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-08-01-preview"
    
    # Prepare request
    headers = {
        "Content-Type": "application/json",
        "api-key": api_key
    }
    
    payload = {
        "messages": [
            {
                "role": "system",
                "content": "You are a precise financial analyst. You analyze bank statements and return ONLY valid JSON with no additional text or formatting."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 1000,
        "temperature": 0.1  # Low temperature for consistent, factual extraction
    }
    
    try:
        logging.info(f"🤖 Calling Azure OpenAI ({deployment})...")
        
        # Make request
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        
        if response.status_code != 200:
            logging.error(f"❌ OpenAI API error: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None
        
        # Parse response
        data = response.json()
        ai_response = data['choices'][0]['message']['content'].strip()
        
        logging.info(f"🤖 AI Response received ({len(ai_response)} chars)")
        
        # Clean response - remove markdown code blocks if present
        ai_response_clean = ai_response
        if "```json" in ai_response:
            ai_response_clean = ai_response.split("```json")[1].split("```")[0].strip()
        elif "```" in ai_response:
            ai_response_clean = ai_response.split("```")[1].split("```")[0].strip()

        # Fix common AI JSON issues: commas in numbers (e.g. 388,281.67 -> 388281.67)
        # Match number patterns with commas that are NOT string values
        ai_response_clean = re.sub(r':\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
            lambda m: ': ' + m.group(1).replace(',', ''), ai_response_clean)

        # Parse JSON
        result = json.loads(ai_response_clean)
        
        # Validate result
        if 'error' in result:
            logging.warning(f"⚠️ AI couldn't extract data: {result.get('error')}")
            return None
        
        total_inflows = float(result.get('total_inflows', 0))
        transaction_count = int(result.get('transaction_count', 0))
        months = int(result.get('months', 1))
        
        if total_inflows <= 0 or transaction_count <= 0:
            logging.warning("⚠️ AI returned zero inflows - statement may be unclear")
            return None
        
        logging.info("="*80)
        logging.info("✅ AI EXTRACTION SUCCESSFUL")
        logging.info("="*80)
        logging.info(f"💰 Total Inflows: {total_inflows:,.2f} EGP")
        logging.info(f"📊 Transactions: {transaction_count}")
        logging.info(f"📅 Months: {months}")
        logging.info("="*80)
        
        return {
            'total_inflows': total_inflows,
            'transaction_count': transaction_count,
            'months': months
        }
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ Failed to parse AI response as JSON: {str(e)}")
        logging.error(f"AI Response was: {ai_response[:500]}")
        return None
        
    except requests.exceptions.Timeout:
        logging.error("❌ OpenAI request timeout")
        return None
        
    except Exception as e:
        logging.error(f"❌ AI analysis error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return None


def analyze_bank_statements(documents):
    """AI-powered bank statement analyzer - works with ANY bank automatically"""
    
    logging.info("="*80)
    logging.info("🤖 AI-POWERED BANK STATEMENT ANALYSIS")
    logging.info("="*80)
    
    # Check if we have bank statements
    bank_statements = documents.get('bank_statements', [])
    if not bank_statements or not isinstance(bank_statements, list):
        logging.info("No bank statements provided for analysis")
        return None
    
    if not document_analysis_client:
        logging.warning("Document Intelligence not configured")
        return None
    
    try:
        total_inflows = 0
        transactions_found = 0
        months_analyzed = 0
        
        for idx, statement in enumerate(bank_statements, 1):
            try:
                # Decode base64 content
                file_content = base64.b64decode(statement.get('content', ''))
                filename = statement.get('filename', f'statement_{idx}.pdf')
                
                logging.info(f"\n--- 🤖 AI Analyzing Bank Statement {idx}: {filename} ---")
                logging.info(f"File size: {len(file_content)} bytes")
                
                # Extract text with Document Intelligence
                file_stream = io.BytesIO(file_content)
                poller = document_analysis_client.begin_analyze_document(
                    "prebuilt-read",  # Best for full text extraction
                    document=file_stream
                )
                
                logging.info("⏳ Extracting text from PDF...")
                result = poller.result()
                page_count = len(result.pages) if result.pages else 0
                logging.info(f"✅ Text extraction complete! Pages extracted: {page_count}")

                if not result.content:
                    logging.warning("⚠️ No text extracted from document")
                    continue

                text_length = len(result.content)
                logging.info(f"📝 Extracted {text_length} characters from {page_count} pages")
                
                # Send to OpenAI for intelligent analysis
                logging.info("🤖 Sending to AI for analysis...")
                ai_result = analyze_with_openai(result.content, filename)
                
                if ai_result:
                    total_inflows += ai_result['total_inflows']
                    transactions_found += ai_result['transaction_count']
                    months_analyzed += ai_result['months']
                    
                    logging.info(f"✅ AI found {ai_result['transaction_count']} transactions = {ai_result['total_inflows']:,.2f} EGP")
                else:
                    logging.warning(f"⚠️ AI analysis failed for statement {idx}")
                
            except Exception as e:
                logging.error(f"❌ Error analyzing statement {idx}: {str(e)}")
                import traceback
                logging.error(traceback.format_exc())
                continue
        
        if months_analyzed == 0 or total_inflows == 0:
            logging.warning("⚠️ No valid transactions found")
            return None
        
        # Calculate averages
        monthly_average = total_inflows / months_analyzed if months_analyzed > 0 else 0
        annual_estimate = monthly_average * 12
        
        logging.info("\n" + "="*80)
        logging.info("🤖 AI ANALYSIS COMPLETE")
        logging.info("="*80)
        logging.info(f"📊 Total Inflows: {total_inflows:,.2f} EGP")
        logging.info(f"📊 Transactions: {transactions_found}")
        logging.info(f"📊 Months Analyzed: {months_analyzed}")
        logging.info(f"📊 Monthly Average: {monthly_average:,.2f} EGP")
        logging.info(f"📊 Annual Estimate: {annual_estimate:,.2f} EGP")
        logging.info("="*80)
        
        return {
            'total_inflows': total_inflows,
            'monthly_average': monthly_average,
            'annual_estimate': annual_estimate,
            'transactions_found': transactions_found,
            'months_analyzed': months_analyzed
        }
        
    except Exception as e:
        logging.error(f"❌ AI bank statement analysis error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return None

def extract_document_text(documents, doc_type):
    """Extract text from a document using Azure Document Intelligence"""
    doc_list = documents.get(doc_type, [])
    if not doc_list or not isinstance(doc_list, list) or not document_analysis_client:
        return None

    all_text = []
    for idx, doc in enumerate(doc_list, 1):
        try:
            file_content = base64.b64decode(doc.get('content', ''))
            filename = doc.get('filename', f'{doc_type}_{idx}.pdf')
            logging.info(f"Extracting text from {doc_type}: {filename} ({len(file_content)} bytes)")

            file_stream = io.BytesIO(file_content)
            poller = document_analysis_client.begin_analyze_document("prebuilt-read", document=file_stream)
            result = poller.result()
            page_count = len(result.pages) if result.pages else 0

            if result.content:
                all_text.append(result.content)
                logging.info(f"Extracted {len(result.content)} chars from {page_count} pages of {filename}")
            else:
                logging.warning(f"No text extracted from {filename}")
        except Exception as e:
            logging.error(f"Error extracting {doc_type} {idx}: {str(e)}")
            continue

    return "\n\n".join(all_text) if all_text else None


def analyze_balance_sheet(documents):
    """Analyze balance sheet using Document Intelligence + OpenAI"""
    text = extract_document_text(documents, 'balance_sheet')
    if not text:
        return None

    api_key = os.environ.get('AZURE_OPENAI_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    deployment = os.environ.get('AZURE_OPENAI_DEPLOYMENT', 'finzeed-chat')
    if not api_key or not endpoint:
        return None

    prompt = f"""You are a financial analyst specialized in analyzing balance sheets for Egyptian SMEs.

TASK: Analyze this balance sheet and extract key financial metrics.

BALANCE SHEET TEXT:
{text[:100000]}

INSTRUCTIONS:
Extract the following information from the balance sheet:
1. Total Assets and their breakdown (current assets, fixed assets)
2. Total Liabilities and their breakdown (current liabilities, long-term liabilities)
3. Owner's Equity / Shareholders' Equity
4. Key financial ratios:
   - Current Ratio (Current Assets / Current Liabilities)
   - Debt-to-Equity Ratio (Total Liabilities / Equity)
   - Working Capital (Current Assets - Current Liabilities)
5. Net Income / Profit if available
6. Cash and cash equivalents
7. Accounts receivable and payable
8. The reporting period/date

The document may be in Arabic or English — handle both.

Return ONLY a valid JSON object (no markdown, no code blocks):
{{
    "total_assets": <number or null>,
    "current_assets": <number or null>,
    "fixed_assets": <number or null>,
    "total_liabilities": <number or null>,
    "current_liabilities": <number or null>,
    "long_term_liabilities": <number or null>,
    "equity": <number or null>,
    "net_income": <number or null>,
    "cash_and_equivalents": <number or null>,
    "accounts_receivable": <number or null>,
    "accounts_payable": <number or null>,
    "current_ratio": <number or null>,
    "debt_to_equity": <number or null>,
    "working_capital": <number or null>,
    "reporting_period": "<date or period string>",
    "currency": "EGP",
    "health_assessment": "strong" or "moderate" or "weak" or "critical",
    "key_observations": [<list of observation strings>]
}}"""

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-08-01-preview"
    headers = {"Content-Type": "application/json", "api-key": api_key}
    payload = {
        "messages": [
            {"role": "system", "content": "You are a precise financial analyst. Analyze balance sheets and return ONLY valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.1
    }

    try:
        logging.info("Calling OpenAI for balance sheet analysis...")
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        if response.status_code != 200:
            logging.error(f"OpenAI balance sheet error: {response.status_code} - {response.text[:200]}")
            return None

        ai_response = response.json()['choices'][0]['message']['content'].strip()
        ai_clean = ai_response
        if "```json" in ai_response:
            ai_clean = ai_response.split("```json")[1].split("```")[0].strip()
        elif "```" in ai_response:
            ai_clean = ai_response.split("```")[1].split("```")[0].strip()

        ai_clean = re.sub(r':\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
            lambda m: ': ' + m.group(1).replace(',', ''), ai_clean)

        result = json.loads(ai_clean)
        logging.info(f"Balance sheet analysis complete: health={result.get('health_assessment')}")
        return result

    except Exception as e:
        logging.error(f"Balance sheet analysis error: {str(e)}")
        return None


def analyze_iscore(documents):
    """Analyze i-Score credit report using Document Intelligence + OpenAI"""
    text = extract_document_text(documents, 'iscore')
    if not text:
        return None

    api_key = os.environ.get('AZURE_OPENAI_KEY')
    endpoint = os.environ.get('AZURE_OPENAI_ENDPOINT')
    deployment = os.environ.get('AZURE_OPENAI_DEPLOYMENT', 'finzeed-chat')
    if not api_key or not endpoint:
        return None

    prompt = f"""You are a credit analyst specialized in analyzing Egyptian i-Score credit reports.

TASK: Analyze this i-Score credit report and extract key credit information.

I-SCORE REPORT TEXT:
{text[:100000]}

INSTRUCTIONS:
Extract the following from the i-Score report:
1. Credit score / rating
2. Number of active credit facilities
3. Total outstanding debt
4. Payment history (on-time vs late payments)
5. Number of credit inquiries
6. Any defaults or delinquencies
7. Credit utilization
8. Oldest and newest account dates
9. Any legal cases or bounced checks

The document may be in Arabic or English — handle both.
i-Score is the Egyptian credit bureau. Scores typically range from 400-850.

Return ONLY a valid JSON object (no markdown, no code blocks):
{{
    "credit_score": <number or null>,
    "score_rating": "excellent" or "good" or "fair" or "poor" or "unknown",
    "active_facilities": <number or null>,
    "total_outstanding_debt": <number or null>,
    "total_credit_limit": <number or null>,
    "credit_utilization_pct": <number or null>,
    "on_time_payments": <number or null>,
    "late_payments": <number or null>,
    "defaults": <number or null>,
    "bounced_checks": <number or null>,
    "legal_cases": <number or null>,
    "credit_inquiries_last_12m": <number or null>,
    "oldest_account": "<date or null>",
    "newest_account": "<date or null>",
    "currency": "EGP",
    "risk_level": "low" or "medium" or "high" or "very_high",
    "key_observations": [<list of observation strings>]
}}"""

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-08-01-preview"
    headers = {"Content-Type": "application/json", "api-key": api_key}
    payload = {
        "messages": [
            {"role": "system", "content": "You are a precise credit analyst. Analyze i-Score reports and return ONLY valid JSON."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 1500,
        "temperature": 0.1
    }

    try:
        logging.info("Calling OpenAI for i-Score analysis...")
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        if response.status_code != 200:
            logging.error(f"OpenAI i-Score error: {response.status_code} - {response.text[:200]}")
            return None

        ai_response = response.json()['choices'][0]['message']['content'].strip()
        ai_clean = ai_response
        if "```json" in ai_response:
            ai_clean = ai_response.split("```json")[1].split("```")[0].strip()
        elif "```" in ai_response:
            ai_clean = ai_response.split("```")[1].split("```")[0].strip()

        ai_clean = re.sub(r':\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
            lambda m: ': ' + m.group(1).replace(',', ''), ai_clean)

        result = json.loads(ai_clean)
        logging.info(f"i-Score analysis complete: score={result.get('credit_score')}, risk={result.get('risk_level')}")
        return result

    except Exception as e:
        logging.error(f"i-Score analysis error: {str(e)}")
        return None


def parse_bank_transactions(text):
    """Parse bank statement text to extract credit transactions"""
    
    total_inflows = 0
    transaction_count = 0
    
    logging.info("Parsing transactions from text...")
    
    # Look for common patterns
    lines = text.split('\n')
    
    for line in lines:
        # Look for credit indicators
        if any(word in line.lower() for word in ['customer payment', 'payment', 'invoice', 'deposit', 'credit', 'cr', 'salary', 'transfer in']):
            # Extract numbers from the line
            numbers = re.findall(r'[\d,]+\.?\d+', line)
            for num_str in numbers:
                try:
                    amount = float(num_str.replace(',', ''))
                    # Only count reasonable transaction amounts (1000 to 10M)
                    if 1000 <= amount <= 10000000:
                        total_inflows += amount
                        transaction_count += 1
                        logging.info(f"  Found: {amount:,.2f} EGP in: {line[:80]}")
                        break  # Only count one amount per line
                except:
                    continue
    
    logging.info(f"✅ Parsed {transaction_count} transactions, total: {total_inflows:,.2f} EGP")
    
    return {
        'total_inflows': total_inflows,
        'transaction_count': transaction_count
    }

_tables_ensured = False

def ensure_new_tables(cursor):
    """Create new tables for the enhanced platform if they don't exist.
    Each statement is executed separately to avoid pyodbc multi-statement issues.
    Only runs once per function app instance."""
    global _tables_ensured
    if _tables_ensured:
        return
    conn = cursor.connection

    table_stmts = [
        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='backoffice_users' AND xtype='U')
        CREATE TABLE backoffice_users (
            id INT IDENTITY PRIMARY KEY,
            email NVARCHAR(255) UNIQUE NOT NULL,
            password_hash NVARCHAR(256),
            password_salt NVARCHAR(64),
            fullname NVARCHAR(255),
            role NVARCHAR(50) DEFAULT 'analyst',
            is_active BIT DEFAULT 1,
            created_at DATETIME DEFAULT GETDATE(),
            last_login DATETIME
        )""",

        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='credit_reports' AND xtype='U')
        CREATE TABLE credit_reports (
            id INT IDENTITY PRIMARY KEY,
            application_id INT,
            ai_recommendation NVARCHAR(50),
            confidence_score DECIMAL(5,2),
            recommended_limit DECIMAL(18,2),
            recommended_tenor INT,
            revenue_analysis NVARCHAR(MAX),
            bank_analysis NVARCHAR(MAX),
            balance_sheet_analysis NVARCHAR(MAX),
            iscore_analysis NVARCHAR(MAX),
            identity_verification NVARCHAR(MAX),
            risk_factors NVARCHAR(MAX),
            positive_factors NVARCHAR(MAX),
            executive_summary NVARCHAR(MAX),
            full_report NVARCHAR(MAX),
            created_at DATETIME DEFAULT GETDATE()
        )""",

        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='application_decisions' AND xtype='U')
        CREATE TABLE application_decisions (
            id INT IDENTITY PRIMARY KEY,
            application_id INT,
            decided_by INT,
            decision NVARCHAR(50),
            final_credit_limit DECIMAL(18,2),
            final_tenor_months INT,
            final_interest_rate DECIMAL(5,2),
            override_reason NVARCHAR(MAX),
            internal_notes NVARCHAR(MAX),
            customer_notified BIT DEFAULT 0,
            decided_at DATETIME DEFAULT GETDATE()
        )""",

        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='insurance_documents' AND xtype='U')
        CREATE TABLE insurance_documents (
            id INT IDENTITY PRIMARY KEY,
            application_id INT,
            document_type NVARCHAR(100),
            filename NVARCHAR(255),
            blob_url NVARCHAR(500),
            uploaded_by INT,
            notes NVARCHAR(MAX),
            uploaded_at DATETIME DEFAULT GETDATE()
        )""",

        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='suppliers' AND xtype='U')
        CREATE TABLE suppliers (
            id INT IDENTITY PRIMARY KEY,
            name NVARCHAR(255) NOT NULL,
            category NVARCHAR(100),
            location NVARCHAR(255),
            invoice_range_min DECIMAL(18,2),
            invoice_range_max DECIMAL(18,2),
            payment_terms NVARCHAR(100),
            status NVARCHAR(50) DEFAULT 'Active',
            is_verified BIT DEFAULT 0,
            created_at DATETIME DEFAULT GETDATE(),
            updated_at DATETIME DEFAULT GETDATE()
        )""",

        """IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='audit_log' AND xtype='U')
        CREATE TABLE audit_log (
            id INT IDENTITY PRIMARY KEY,
            user_id INT,
            user_type NVARCHAR(20),
            action NVARCHAR(100),
            entity_type NVARCHAR(50),
            entity_id INT,
            details NVARCHAR(MAX),
            ip_address NVARCHAR(50),
            created_at DATETIME DEFAULT GETDATE()
        )""",

        """IF NOT EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'applications' AND COLUMN_NAME = 'application_status'
        )
        ALTER TABLE applications ADD application_status NVARCHAR(50) DEFAULT 'submitted'""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='customer_notified')
        ALTER TABLE applications ADD customer_notified BIT DEFAULT 0""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='notification_sent_at')
        ALTER TABLE applications ADD notification_sent_at DATETIME""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='requested_amount')
        ALTER TABLE applications ADD requested_amount DECIMAL(18,2)""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='requested_installments')
        ALTER TABLE applications ADD requested_installments INT""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='national_id_number')
        ALTER TABLE applications ADD national_id_number NVARCHAR(50)""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='years_in_business')
        ALTER TABLE applications ADD years_in_business INT""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='business_description')
        ALTER TABLE applications ADD business_description NVARCHAR(MAX)""",

        """IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='applications' AND COLUMN_NAME='declared_monthly_inflows')
        ALTER TABLE applications ADD declared_monthly_inflows DECIMAL(18,2)""",
    ]

    for stmt in table_stmts:
        try:
            cursor.execute(stmt)
            conn.commit()
        except Exception as e:
            logging.warning(f"Schema stmt skipped: {str(e)[:100]}")
            try:
                conn.rollback()
            except:
                pass

    _tables_ensured = True
    logging.info("Database schema check completed")


def analyze_with_claude(data, bank_analysis, balance_sheet_data=None, iscore_data=None):
    """
    Use Anthropic Claude to perform comprehensive credit risk analysis.

    Claude receives all extracted data and generates a structured credit report
    with recommendation, risk factors, and executive summary.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key or not anthropic_sdk:
        logging.warning("Anthropic Claude not configured — falling back to rule-based assessment")
        return None

    try:
        client = anthropic_sdk.Anthropic(api_key=api_key)

        # Build the data summary for Claude
        revenue = data.get('revenue', 0)
        if isinstance(revenue, str):
            try:
                revenue = float(revenue.replace(',', ''))
            except:
                revenue = 0

        company = data.get('company', 'Unknown')
        industry = data.get('industry', 'Not specified')
        firstname = data.get('firstname', '')
        lastname = data.get('lastname', '')
        years_in_business = data.get('years_in_business', 'Not provided')
        business_description = data.get('business_description', 'Not provided')
        requested_amount = data.get('requested_amount', 'Not specified')
        requested_installments = data.get('requested_installments', 'Not specified')

        # Bank analysis summary
        bank_summary = "No bank statements analyzed."
        if bank_analysis:
            bank_summary = f"""Bank Statement Analysis (via OpenAI extraction):
- Total Inflows: {bank_analysis.get('total_inflows', 0):,.2f} EGP
- Monthly Average: {bank_analysis.get('monthly_average', 0):,.2f} EGP
- Annual Estimate: {bank_analysis.get('annual_estimate', 0):,.2f} EGP
- Transactions Found: {bank_analysis.get('transactions_found', 0)}
- Months Analyzed: {bank_analysis.get('months_analyzed', 0)}"""

        # Balance sheet summary
        balance_sheet_summary = "No balance sheet provided."
        if balance_sheet_data:
            balance_sheet_summary = f"""Balance Sheet Analysis (via OpenAI extraction):
- Total Assets: {balance_sheet_data.get('total_assets', 'N/A')} EGP
- Current Assets: {balance_sheet_data.get('current_assets', 'N/A')} EGP
- Total Liabilities: {balance_sheet_data.get('total_liabilities', 'N/A')} EGP
- Current Liabilities: {balance_sheet_data.get('current_liabilities', 'N/A')} EGP
- Equity: {balance_sheet_data.get('equity', 'N/A')} EGP
- Net Income: {balance_sheet_data.get('net_income', 'N/A')} EGP
- Current Ratio: {balance_sheet_data.get('current_ratio', 'N/A')}
- Debt-to-Equity: {balance_sheet_data.get('debt_to_equity', 'N/A')}
- Working Capital: {balance_sheet_data.get('working_capital', 'N/A')} EGP
- Cash: {balance_sheet_data.get('cash_and_equivalents', 'N/A')} EGP
- Health Assessment: {balance_sheet_data.get('health_assessment', 'N/A')}
- Reporting Period: {balance_sheet_data.get('reporting_period', 'N/A')}
- Key Observations: {', '.join(balance_sheet_data.get('key_observations', []))}"""

        # i-Score summary
        iscore_summary = "No i-Score report provided."
        if iscore_data:
            iscore_summary = f"""i-Score Credit Report Analysis (via OpenAI extraction):
- Credit Score: {iscore_data.get('credit_score', 'N/A')}
- Score Rating: {iscore_data.get('score_rating', 'N/A')}
- Risk Level: {iscore_data.get('risk_level', 'N/A')}
- Active Credit Facilities: {iscore_data.get('active_facilities', 'N/A')}
- Total Outstanding Debt: {iscore_data.get('total_outstanding_debt', 'N/A')} EGP
- Credit Utilization: {iscore_data.get('credit_utilization_pct', 'N/A')}%
- On-Time Payments: {iscore_data.get('on_time_payments', 'N/A')}
- Late Payments: {iscore_data.get('late_payments', 'N/A')}
- Defaults: {iscore_data.get('defaults', 'N/A')}
- Bounced Checks: {iscore_data.get('bounced_checks', 'N/A')}
- Legal Cases: {iscore_data.get('legal_cases', 'N/A')}
- Credit Inquiries (12m): {iscore_data.get('credit_inquiries_last_12m', 'N/A')}
- Key Observations: {', '.join(iscore_data.get('key_observations', []))}"""

        prompt = f"""You are Finzeed's AI Credit Analyst. Analyze this SME credit application and produce a structured credit assessment report.

## Applicant Information
- Company: {company}
- Applicant: {firstname} {lastname}
- Industry: {industry}
- Years in Business: {years_in_business}
- Business Description: {business_description}
- Declared Annual Revenue: {revenue:,.2f} EGP
- Requested Credit Amount: {requested_amount}
- Requested Installments: {requested_installments}

## {bank_summary}

## {balance_sheet_summary}

## {iscore_summary}

## Finzeed Credit Parameters
- Credit range: EGP 100,000 to 5,000,000
- Interest rate: 3.5% per month (42% per annum)
- Tenor options: 45 days, 3, 6, 9, 12 months
- Revenue tiers: Tier 1 (10M+), Tier 2 (5M-10M), Tier 3 (3M-5M), Below threshold (<3M)
- Maximum credit: 15% of verified annual revenue (capped at 5M)
- Revenue discrepancy >30% triggers manual review

## Your Task
Analyze all available data and return ONLY a valid JSON object (no markdown, no code blocks) with this exact structure:

{{
  "recommendation": "APPROVE" or "REJECT" or "REVIEW",
  "confidence_score": <number 0-100>,
  "recommended_credit_limit": <number in EGP>,
  "recommended_tenor_months": <number>,
  "monthly_rate": 3.5,
  "revenue_analysis": {{
    "declared_annual": <number>,
    "verified_annual": <number or null if no bank data>,
    "discrepancy_percent": <number or null>,
    "monthly_trend": "growing" or "stable" or "declining" or "unknown",
    "revenue_verdict": "VERIFIED" or "UNVERIFIED" or "DISCREPANCY"
  }},
  "bank_analysis": {{
    "total_inflows": <number>,
    "total_outflows": <number or null>,
    "avg_monthly_balance": <number or null>,
    "transactions_analyzed": <number>,
    "months_covered": <number>,
    "bounce_count": <number or null>,
    "seasonal_pattern": <string or null>
  }},
  "balance_sheet_analysis": {{
    "financial_health": "strong" or "moderate" or "weak" or "N/A",
    "current_ratio_verdict": "<assessment string>",
    "debt_level_verdict": "<assessment string>",
    "key_concerns": [<list of strings>]
  }},
  "iscore_analysis": {{
    "credit_score": <number or null>,
    "risk_level": "low" or "medium" or "high" or "very_high" or "N/A",
    "payment_history_verdict": "<assessment string>",
    "key_concerns": [<list of strings>]
  }},
  "risk_factors": [<list of risk strings>],
  "positive_factors": [<list of positive strings>],
  "executive_summary": "<2-3 sentence summary for the credit team>"
}}

Be conservative with approvals. If data is insufficient, recommend REVIEW. Never recommend more than 15% of verified annual revenue or 5M EGP."""

        logging.info("Calling Anthropic Claude for credit analysis...")

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        ai_response = message.content[0].text.strip()
        logging.info(f"Claude response received ({len(ai_response)} chars)")

        # Clean response
        ai_response_clean = ai_response
        if "```json" in ai_response:
            ai_response_clean = ai_response.split("```json")[1].split("```")[0].strip()
        elif "```" in ai_response:
            ai_response_clean = ai_response.split("```")[1].split("```")[0].strip()

        # Fix comma-formatted numbers
        ai_response_clean = re.sub(r':\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)',
            lambda m: ': ' + m.group(1).replace(',', ''), ai_response_clean)

        report = json.loads(ai_response_clean)
        logging.info(f"Claude recommendation: {report.get('recommendation')} (confidence: {report.get('confidence_score')})")
        return report

    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse Claude response as JSON: {str(e)}")
        return None
    except Exception as e:
        logging.error(f"Claude analysis error: {str(e)}")
        import traceback
        logging.error(traceback.format_exc())
        return None


def save_credit_report(application_id, claude_report):
    """Save Claude's credit report to the credit_reports table"""
    if not claude_report or not application_id:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ensure_new_tables(cursor)

        cursor.execute("""
            INSERT INTO credit_reports (
                application_id, ai_recommendation, confidence_score,
                recommended_limit, recommended_tenor,
                revenue_analysis, bank_analysis,
                balance_sheet_analysis, iscore_analysis,
                risk_factors, positive_factors, executive_summary, full_report
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            application_id,
            claude_report.get('recommendation', 'REVIEW'),
            claude_report.get('confidence_score', 0),
            claude_report.get('recommended_credit_limit', 0),
            claude_report.get('recommended_tenor_months', 6),
            json.dumps(claude_report.get('revenue_analysis', {})),
            json.dumps(claude_report.get('bank_analysis', {})),
            json.dumps(claude_report.get('balance_sheet_analysis', {})),
            json.dumps(claude_report.get('iscore_analysis', {})),
            json.dumps(claude_report.get('risk_factors', [])),
            json.dumps(claude_report.get('positive_factors', [])),
            claude_report.get('executive_summary', ''),
            json.dumps(claude_report)
        ))
        conn.commit()
        cursor.close()
        conn.close()
        logging.info(f"Credit report saved for application {application_id}")
    except Exception as e:
        logging.error(f"Error saving credit report: {str(e)}")


def log_audit(user_id, user_type, action, entity_type, entity_id, details=None, ip_address=None):
    """Write an entry to the audit log"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_log (user_id, user_type, action, entity_type, entity_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, user_type, action, entity_type, entity_id, details, ip_address))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.warning(f"Audit log error: {str(e)}")


def perform_ai_assessment(data):
    """Perform AI credit assessment with bank statement verification"""
    
    revenue = data.get('revenue', 0)
    mobile = data.get('mobile', '')
    documents = data.get('documents', {})
    
    if isinstance(revenue, str):
        try:
            revenue = float(revenue.replace(',', ''))
        except:
            revenue = 0
    
    # Analyze all documents
    bank_analysis = analyze_bank_statements(documents)
    balance_sheet_analysis = analyze_balance_sheet(documents)
    iscore_analysis = analyze_iscore(documents)

    if balance_sheet_analysis:
        logging.info(f"Balance sheet: health={balance_sheet_analysis.get('health_assessment')}")
    if iscore_analysis:
        logging.info(f"i-Score: score={iscore_analysis.get('credit_score')}, risk={iscore_analysis.get('risk_level')}")

    revenue_verification_note = None
    revenue_mismatch = False
    verified_revenue = revenue
    
    if bank_analysis:
        declared_annual = revenue
        analyzed_annual = bank_analysis['annual_estimate']
        
        if declared_annual > 0:
            discrepancy = abs(declared_annual - analyzed_annual) / declared_annual * 100
            
            logging.info(f"\n{'='*80}")
            logging.info("REVENUE VERIFICATION")
            logging.info(f"{'='*80}")
            logging.info(f"Declared: {declared_annual:,.2f} EGP")
            logging.info(f"Verified: {analyzed_annual:,.2f} EGP")
            logging.info(f"Discrepancy: {discrepancy:.1f}%")
            logging.info(f"{'='*80}\n")
            
            if discrepancy > 30:
                revenue_mismatch = True
                revenue_verification_note = f"⚠️ Revenue discrepancy: Declared {declared_annual:,.0f} EGP vs Verified {analyzed_annual:,.0f} EGP ({discrepancy:.0f}% difference)"
            else:
                revenue_verification_note = f"✅ Revenue verified: {analyzed_annual:,.0f} EGP from bank statements"
                verified_revenue = analyzed_annual
        
        revenue_verification_note = f"{revenue_verification_note}. Analyzed {bank_analysis['transactions_found']} transactions over {bank_analysis['months_analyzed']} months."
    else:
        revenue_verification_note = "⏳ Bank statement analysis pending - documents will be manually reviewed"
    
    assessment_revenue = verified_revenue
    
    # Thresholds
    TIER_1_THRESHOLD = 10000000  # 10M+
    TIER_2_THRESHOLD = 5000000   # 5M+
    TIER_3_THRESHOLD = 3000000   # 3M+
    
    MONTHLY_INTEREST_RATE = 3.5
    ANNUAL_INTEREST_RATE = MONTHLY_INTEREST_RATE * 12
    
    # Assessment logic
    if revenue_mismatch:
        decision = "UNDER_REVIEW"
        credit_limit = 0
        tenor_months = 6
        interest_rate = ANNUAL_INTEREST_RATE
        confidence = 30
        risk_factors = ["Revenue discrepancy detected", "Manual verification required"]
        recommendations = [
            revenue_verification_note,
            f"Our team will contact you at {mobile} within 24-48 hours"
        ]
    elif assessment_revenue >= TIER_1_THRESHOLD:
        decision = "APPROVED"
        credit_limit = min(assessment_revenue * 0.15, 5000000)
        tenor_months = 18
        interest_rate = ANNUAL_INTEREST_RATE
        confidence = 85
        risk_factors = ["Strong revenue base", "Excellent payment capacity"]
        recommendations = [
            f"Approved for {tenor_months} months",
            f"Interest: {MONTHLY_INTEREST_RATE}% monthly ({ANNUAL_INTEREST_RATE}% annual)",
            revenue_verification_note
        ]
    elif assessment_revenue >= TIER_2_THRESHOLD:
        decision = "APPROVED"
        credit_limit = min(assessment_revenue * 0.12, 3000000)
        tenor_months = 12
        interest_rate = ANNUAL_INTEREST_RATE
        confidence = 75
        risk_factors = ["Good revenue base", "Standard risk profile"]
        recommendations = [
            f"Approved for {tenor_months} months",
            f"Interest: {MONTHLY_INTEREST_RATE}% monthly ({ANNUAL_INTEREST_RATE}% annual)",
            revenue_verification_note
        ]
    elif assessment_revenue >= TIER_3_THRESHOLD:
        decision = "APPROVED"
        credit_limit = min(assessment_revenue * 0.10, 1500000)
        tenor_months = 6
        interest_rate = ANNUAL_INTEREST_RATE
        confidence = 65
        risk_factors = ["Moderate revenue", "Standard approval"]
        recommendations = [
            f"Approved for {tenor_months} months",
            f"Interest: {MONTHLY_INTEREST_RATE}% monthly ({ANNUAL_INTEREST_RATE}% annual)",
            revenue_verification_note
        ]
    else:
        decision = "UNDER_REVIEW"
        credit_limit = 0
        tenor_months = 6
        interest_rate = ANNUAL_INTEREST_RATE
        confidence = 40
        risk_factors = ["Revenue below 3M threshold", "Manual review needed"]
        recommendations = [
            f"Our team will contact you at {mobile} within 24-48 hours",
            revenue_verification_note if revenue_verification_note else "Additional documentation may be requested"
        ]
    
    recommendations = [r for r in recommendations if r is not None]

    # ===== DUAL-AI: Send all data to Claude for comprehensive analysis =====
    claude_report = analyze_with_claude(data, bank_analysis, balance_sheet_analysis, iscore_analysis)

    if claude_report:
        # Claude successfully analyzed — use its recommendation for internal report
        # but customer always sees "submitted" (decision hidden from customer)
        logging.info(f"Claude AI recommendation: {claude_report.get('recommendation')} "
                     f"(confidence: {claude_report.get('confidence_score')})")
    else:
        logging.info("Claude unavailable — using rule-based assessment only")

    return {
        'decision': decision,
        'credit_limit': credit_limit,
        'tenor_months': tenor_months,
        'interest_rate': interest_rate,
        'confidence_score': confidence,
        'risk_factors': risk_factors,
        'recommendations': recommendations,
        'bank_analysis': bank_analysis,
        'balance_sheet_analysis': balance_sheet_analysis,
        'iscore_analysis': iscore_analysis,
        'claude_report': claude_report
    }