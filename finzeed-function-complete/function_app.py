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

def get_db_connection():
    """Get database connection"""
    connection_string = os.environ.get('SQL_CONNECTION_STRING')
    if not connection_string:
        raise Exception("SQL_CONNECTION_STRING not found in environment variables")

    return pyodbc.connect(connection_string)

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

        # Send verification email
        email_sent = send_verification_email(email, verification_token, firstname)

        return make_response({
            "success": True,
            "email_verification_required": True,
            "email_sent": email_sent,
            "message": "Account created! Please check your email to verify your account before signing in."
        }, 201)

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

        # Get application history
        applications = []
        try:
            cursor.execute("""
                SELECT id, company_name, ai_decision, ai_credit_limit, ai_tenor_months,
                       ai_confidence_score, ai_assessed_at
                FROM applications WHERE user_id = ?
                ORDER BY ai_assessed_at DESC
            """, (payload['user_id'],))
            for app_row in cursor.fetchall():
                applications.append({
                    "id": app_row[0],
                    "company_name": app_row[1] or '',
                    "decision": app_row[2] or '',
                    "credit_limit": float(app_row[3]) if app_row[3] else 0,
                    "tenor_months": app_row[4] or 0,
                    "confidence_score": float(app_row[5]) if app_row[5] else 0,
                    "assessed_at": str(app_row[6]) if app_row[6] else ''
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

# ========== EXISTING HANDLERS ==========

def save_application_to_db(data, ai_assessment):
    """Save application to database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
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
        
        # 2. Insert application
        cursor.execute("""
            INSERT INTO applications (
                user_id, company_name, firstname, lastname, email, mobile,
                industry, annual_revenue, purpose, status,
                ai_decision, ai_credit_limit, ai_tenor_months, ai_interest_rate,
                ai_confidence_score, ai_risk_factors, ai_recommendations, ai_assessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
        """, (
            user_id, data['company'], data['firstname'], data['lastname'],
            data['email'], data['mobile'], data.get('industry'), 
            data.get('revenue'), data.get('purpose'),
            'ai_assessment',
            ai_assessment.get('decision'),
            ai_assessment.get('credit_limit'),
            ai_assessment.get('tenor_months'),
            ai_assessment.get('interest_rate'),
            ai_assessment.get('confidence_score'),
            json.dumps(ai_assessment.get('risk_factors', [])),
            json.dumps(ai_assessment.get('recommendations', []))
        ))
        
        # Get application_id
        cursor.execute("SELECT @@IDENTITY")
        application_id = cursor.fetchone()[0]
        
        # 3. Insert documents if provided
        documents = data.get('documents', {})
        for doc_type, doc_info in documents.items():
            if doc_info and isinstance(doc_info, dict) and 'blob_url' in doc_info:
                cursor.execute("""
                    INSERT INTO documents (application_id, document_type, filename, blob_url, file_size)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    application_id,
                    doc_type,
                    doc_info.get('filename', ''),
                    doc_info.get('blob_url', ''),
                    doc_info.get('file_size', 0)
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
        
        logging.info(f"Form data - Company: {company}, Email: {email}, Revenue: {revenue}")
        
        # Convert revenue to float
        try:
            revenue = float(revenue.replace(',', '')) if revenue else 0
        except:
            revenue = 0
        
        # Get uploaded files
        bank_statements = req.files.getlist('bank_statements') or req.files.getlist('bankStatements') or []
        
        logging.info(f"Received {len(bank_statements)} bank statement(s)")
        
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
            'documents': {
                'bank_statements': []
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
        
        # Perform AI assessment with bank analysis
        ai_assessment = perform_ai_assessment(data)
        
        # Save to database
        db_result = save_application_to_db(data, ai_assessment)
        
        # Prepare response
        response_data = {
            'decision': ai_assessment['decision'],
            'credit_limit': ai_assessment['credit_limit'],
            'tenor_months': ai_assessment['tenor_months'],
            'interest_rate': ai_assessment['interest_rate'],
            'confidence_score': ai_assessment['confidence_score'],
            'risk_factors': ai_assessment['risk_factors'],
            'recommendations': ai_assessment['recommendations'],
            'application_id': db_result.get('application_id'),
            'bank_analysis': ai_assessment.get('bank_analysis')
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
            json.dumps({"error": "An error occurred processing your documents. Please try again."}),
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
        # Perform AI assessment
        ai_assessment = perform_ai_assessment(req_body)
        
        # Save to database
        db_result = save_application_to_db(req_body, ai_assessment)
        
        # Prepare response
        response_data = {
            'decision': ai_assessment['decision'],
            'credit_limit': ai_assessment['credit_limit'],
            'tenor_months': ai_assessment['tenor_months'],
            'interest_rate': ai_assessment['interest_rate'],
            'confidence_score': ai_assessment['confidence_score'],
            'risk_factors': ai_assessment['risk_factors'],
            'recommendations': ai_assessment['recommendations'],
            'application_id': db_result.get('application_id'),
            'bank_analysis': ai_assessment.get('bank_analysis')
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
            json.dumps({"error": "An error occurred processing your application. Please try again."}),
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
    
    # Prepare the prompt
    prompt = f"""You are a financial analyst specialized in analyzing bank statements for Egyptian SMEs.

TASK: Analyze this bank statement and extract ONLY the following information.

BANK STATEMENT TEXT:
{text_content[:15000]}

INSTRUCTIONS:
1. Extract TOTAL INFLOWS (money coming INTO the account):
   - Include: deposits, incoming transfers, customer payments, sales revenue, wire transfers IN, "IPN Inward", "Cash Deposit", "Account Transfer Collection"
   - Exclude: withdrawals, ATM, outgoing transfers, fees, purchases, money going OUT, "Outward", "POS Purchase"
   
2. Count the NUMBER of inflow transactions

3. Determine the TIME PERIOD covered (how many months of data)

IMPORTANT RULES:
- Only count INFLOWS (credits/deposits/incoming money)
- Skip OUTFLOWS (debits/withdrawals/outgoing money)  
- Ignore account balances (we only want transaction amounts)
- The statement may be in Arabic or English or mixed - handle both
- Look for transaction patterns and sum ALL inflow amounts

Return ONLY a valid JSON object with this exact structure (no markdown, no explanation, no code blocks):
{{
    "total_inflows": <total number in EGP>,
    "transaction_count": <number of inflow transactions>,
    "months": <number of months covered>,
    "currency": "EGP"
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
        "max_tokens": 500,
        "temperature": 0.1  # Low temperature for consistent, factual extraction
    }
    
    try:
        logging.info(f"🤖 Calling Azure OpenAI ({deployment})...")
        
        # Make request
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        
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
                logging.info("✅ Text extraction complete!")
                
                if not result.content:
                    logging.warning("⚠️ No text extracted from document")
                    continue
                
                text_length = len(result.content)
                logging.info(f"📝 Extracted {text_length} characters")
                
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
    
    # Analyze bank statements
    bank_analysis = analyze_bank_statements(documents)
    
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
    
    return {
        'decision': decision,
        'credit_limit': credit_limit,
        'tenor_months': tenor_months,
        'interest_rate': interest_rate,
        'confidence_score': confidence,
        'risk_factors': risk_factors,
        'recommendations': recommendations,
        'bank_analysis': bank_analysis
    }