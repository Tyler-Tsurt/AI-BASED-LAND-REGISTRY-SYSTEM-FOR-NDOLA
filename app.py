import io

import secrets
import time


import os
import json
import time
import secrets
import hashlib
import re
import threading

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.units import inch
from reportlab.lib.colors import black, blue, red, green
from flask import send_file

from datetime import datetime
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, jsonify, current_app
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from shapely.geometry import shape
from geoalchemy2.shape import from_shape, to_shape
from sqlalchemy import func, or_
from models import db, User, LandApplication, Document, LandParcel, LandConflict, SystemSettings, AuditLog, NotificationLog, AvailableLand
from ai_conflict_enhanced import detect_conflicts_from_documents
from document_processing import extract_document_text
from validation_utils import (
    validate_nrc, validate_tpin, validate_phone, validate_email,
    validate_all_application_data, quick_validate, normalize_identifier
)
from duplicate_detector import detect_all_duplicates, check_identity_duplicate
import json
import time
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load environment variables from .env file
load_dotenv()

# --- App & Config ---
app = Flask(__name__)

# Must exist in .env
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "postgresql://user:pw@localhost/dbname")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Expose a few safe Python builtins to Jinja templates so templates can call
# `min()`, `max()` and `range()` directly (used for progress bars and pagination).
app.jinja_env.globals.update(min=min, max=max, range=range)

# File upload configuration
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)  # ensure folder exists
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

# Initialize database
db.init_app(app)

# --- Login Manager ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    """Loads a user from the database by their ID."""
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


# --- Helper Functions ---
def _allowed_filename(filename):
    """Check if a file's extension is in the set of allowed extensions."""
    _, ext = os.path.splitext(filename.lower())
    return ext.replace('.', '') in ALLOWED_EXTENSIONS



def _save_upload(fileobj, user_id, app_id, field_name):
    """
    Save uploaded file to LOCAL filesystem.
    Returns the relative filepath.
    """
    if not fileobj or fileobj.filename == '':
        return None

    fname = secure_filename(fileobj.filename)
    if not _allowed_filename(fname):
        raise ValueError(f"File type not allowed: {fname}")

    # Create directory structure: uploads/user_{user_id}/app_{app_id}/
    user_dir = os.path.join(app.config['UPLOAD_FOLDER'], f"user_{user_id}")
    app_dir = os.path.join(user_dir, f"app_{app_id}")
    
    os.makedirs(app_dir, exist_ok=True)

    timestamp = int(time.time())
    random_token = secrets.token_hex(6)
    unique_filename = f"{field_name}-{timestamp}-{random_token}-{fname}"
    
    # Full file path
    filepath = os.path.join(app_dir, unique_filename)
    
    try:
        fileobj.save(filepath)
    except Exception as e:
        raise Exception(f"Failed to save file: {str(e)}")
    
    return unique_filename



def log_audit(action, table_name, record_id, old_values=None, new_values=None):
    """Logs an audit event to the AuditLog model."""
    try:
        audit_log = AuditLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            table_name=table_name,
            record_id=record_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=request.remote_addr,
            user_agent=request.user_agent.string
        )
        db.session.add(audit_log)
        db.session.commit()
    except Exception:
        # If audit logging fails, don't break the main flow.
        current_app.logger.exception("Failed to write audit log")


# --- Routes ---
@app.route('/')
@app.route('/index')
def index():
    """Renders the index page."""
    return render_template('index.html')


@app.route('/api/process_payment', methods=['POST'])
def api_process_payment():
    """Mock payment endpoint for local testing.

    Expected JSON: { amount: number, method: string, metadata: {...}, mobile_reference?: string }
    Returns: { success: bool, transaction_id?: str, message?: str }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'success': False, 'message': 'Invalid JSON payload'}), 400


    amount = data.get('amount')
    method = data.get('method')
    mobile_ref = data.get('mobile_reference')
    mobile_phone = data.get('mobile_phone')

    if not method or not amount:
        return jsonify({'success': False, 'message': 'Missing payment method or amount'}), 400

    # Mobile money: accept either a mobile_reference OR a mobile_phone to initiate a mock transaction
    if method in ('mtn', 'airtel', 'zamtel') and not (mobile_ref or mobile_phone):
        return jsonify({'success': False, 'message': 'Provide mobile wallet phone number or transaction reference'}), 400

    # Simulate processing delay
    time.sleep(0.6)

    # Simulate success and return a synthetic transaction id
    tx = f"TX-{int(time.time())}-{secrets.token_hex(4)}"
    return jsonify({'success': True, 'transaction_id': tx})


@app.route('/api/ai_validate', methods=['POST'])
def api_ai_validate():
    """AI-powered validation endpoint. Accepts {field, value} and returns {valid, message}.

    Fields supported: 'nrc', 'tpin', 'email', 'phone'
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'valid': False, 'message': 'Invalid JSON'}), 400

    field = (payload.get('field') or '').lower()
    value = (payload.get('value') or '').strip()

    if not field or not value:
        return jsonify({'valid': False, 'message': 'Missing field or value'}), 400

    # Use comprehensive validation
    result = quick_validate(field, value)
    return jsonify(result)


@app.route('/api/digital_clearance', methods=['POST'])
def api_digital_clearance():
    """Simulated Digital Clearance API.

    Expects JSON: { identifier: string } (NRC or Passport)
    Returns: { status: 'Pending'|'In Progress'|'Complete' }
    Deterministic simulation based on hash of identifier.
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'status': 'Pending', 'message': 'Invalid JSON'}), 400

    identifier = (payload.get('identifier') or '').strip()
    if not identifier:
        return jsonify({'status': 'Pending', 'message': 'Missing identifier'}), 400

    # deterministic pseudo-randomness: sum of ords mod 3
    s = sum(ord(c) for c in identifier) % 3
    status = ['Pending', 'In Progress', 'Complete'][s]
    return jsonify({'status': status})


@app.route('/api/verify_payment', methods=['POST'])
def api_verify_payment():
    """Simulated payment verification endpoint.

    Accepts JSON: { transaction_id: str }
    Returns: { verified: bool, status: 'Approved'|'Failed' }
    Deterministic: approve when last hex char in tx id is an even digit/hex.
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'verified': False, 'status': 'Failed', 'message': 'Invalid JSON'}), 400

    tx = (payload.get('transaction_id') or '').strip()
    if not tx:
        return jsonify({'verified': False, 'status': 'Failed', 'message': 'Missing transaction_id'}), 400

    last = tx[-1]
    try:
        v = int(last, 16)
        approved = (v % 2 == 0)
    except Exception:
        approved = False

    return jsonify({'verified': approved, 'status': 'Approved' if approved else 'Failed'})





@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login with role-based redirection."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            if not user.is_active:
                flash('Account deactivated. Contact support.', 'danger')
                return redirect(url_for('login'))
            
            user.last_login = datetime.utcnow()
            db.session.commit()
            
            login_user(user)
            
            log_audit('user_login', 'users', user.id, None, {
                'username': username,
                'role': user.role
            })
            
            # CRITICAL: Role-based redirect
            if user.role == 'seller':
                return redirect(url_for('seller_dashboard'))
            elif user.role in ['admin', 'super_admin']:
                return redirect(url_for('admin_seller_listings'))
            else:  # citizen
                return redirect(url_for('index'))
        else:
            flash('Invalid credentials.', 'danger')
    
    return render_template('login.html')


@app.route('/logout')
@login_required


def logout():
    """Logs out the current user."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))



def citizen_required(f):
    """Decorator to require citizen role for land registration."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in.', 'warning')
            return redirect(url_for('login'))
        # Only citizens can register land
        if current_user.role != 'citizen':
            flash('Only citizens can apply for land registration.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function






@app.route("/admin_dashboard")
@login_required
def admin_dashboard():
    if current_user.role not in ["admin", "super_admin"]:
        flash("You do not have permission to view this page.", "danger")
        return redirect(url_for("index"))
    
    # Only show real applications tied to a user
    base_query = LandApplication.query.filter(LandApplication.user_id.isnot(None))

    if current_user.role == "admin":
        base_query = base_query.filter(LandApplication.reviewed_by == current_user.id)

    # Search functionality
    search = request.args.get('search', '').strip()
    if search:
        search_filter = or_(
            LandApplication.reference_number.ilike(f'%{search}%'),
            LandApplication.applicant_name.ilike(f'%{search}%'),
            LandApplication.land_location.ilike(f'%{search}%'),
            LandApplication.nrc_number.ilike(f'%{search}%')
        )
        base_query = base_query.filter(search_filter)
    
    # Filter by status
    status_filter = request.args.get('filter', '').strip()
    if status_filter and status_filter in ['pending', 'approved', 'conflict', 'rejected']:
        base_query = base_query.filter(LandApplication.status == status_filter)

    # Pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)






    # Counts for dashboard stats (using the original base query without filters)
    stats_query = LandApplication.query.filter(LandApplication.user_id.isnot(None))
    if current_user.role == "admin":
        stats_query = stats_query.filter(LandApplication.reviewed_by == current_user.id)
    
    total_count = stats_query.count()
    pending_count = stats_query.filter_by(status="pending").count()
    approved_count = stats_query.filter_by(status="approved").count()
    conflict_count = stats_query.filter_by(status="conflict").count()
    rejected_count = stats_query.filter_by(status="rejected").count()

    # Paginate the applications list for display
    pagination = base_query.order_by(LandApplication.submitted_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    applications = pagination.items

    # AI alerts - unresolved conflicts only
    ai_alerts = {
        "document_duplicates": db.session.query(LandConflict).filter_by(conflict_type='document_duplicate', status='unresolved').count(),
        "content_duplicates": db.session.query(LandConflict).filter_by(conflict_type='content_duplicate', status='unresolved').count(),
        "spatial_overlaps": db.session.query(LandConflict).filter_by(conflict_type='spatial_overlap', status='unresolved').count(),
        "fraud_alerts": db.session.query(LandConflict).filter(
            LandConflict.conflict_type.in_(['document_duplicate', 'content_duplicate']),
            LandConflict.confidence_score >= 0.85,
            LandConflict.status == 'unresolved'
        ).count()
    }

    # --- Collect conflict summaries for the displayed page of applications ---
    application_ids = [a.id for a in applications]
    conflicts_map = {}
    if application_ids:
        conflicts = LandConflict.query.filter(LandConflict.application_id.in_(application_ids)).all()
        for c in conflicts:
            lst = conflicts_map.setdefault(c.application_id, [])
            conflict_data = {
                'id': c.id, 
                'type': c.conflict_type, 
                'confidence': c.confidence_score, 
                'title': c.title,
                'status': c.status
            }
            # Add the conflicting application ID if available
            if c.conflicting_parcel_id:
                # Find the application associated with this parcel
                conflicting_app = LandApplication.query.filter_by(
                    id=db.session.query(LandParcel.application_id).filter_by(id=c.conflicting_parcel_id).scalar_subquery()
                ).first()
                if conflicting_app:
                    conflict_data['conflicting_app_id'] = conflicting_app.id
            lst.append(conflict_data)

    # --- Collect document counts and duplicate indicators for the displayed apps ---
    docs_summary = {}
    if application_ids:
        docs = Document.query.filter(Document.application_id.in_(application_ids)).all()
        # group docs by application
        docs_by_app = {}
        hashes = set()
        for d in docs:
            docs_by_app.setdefault(d.application_id, []).append(d)
            if getattr(d, 'file_hash', None):
                hashes.add(d.file_hash)

        # find duplicate counts for this set of hashes across the DB
        counts_by_hash = {}
        if hashes:
            rows = db.session.query(Document.file_hash, func.count(Document.id)).filter(Document.file_hash.in_(list(hashes))).group_by(Document.file_hash).all()
            for fh, cnt in rows:
                counts_by_hash[fh] = cnt

        for aid, dlist in docs_by_app.items():
            total_docs = len(dlist)
            duplicate_docs = 0
            for d in dlist:
                fh = getattr(d, 'file_hash', None)
                if fh and counts_by_hash.get(fh, 0) > 1:
                    duplicate_docs += 1
            docs_summary[aid] = {'total': total_docs, 'duplicates': duplicate_docs}

    return render_template(
        "admin_dashboard_improved.html",
        applications=applications,
        total_count=total_count,
        pending_count=pending_count,
        approved_count=approved_count,
        conflict_count=conflict_count,
        rejected_count=rejected_count,
        ai_alerts=ai_alerts,
        pagination=pagination,
        conflicts_map=conflicts_map,
        docs_summary=docs_summary,
    )


@app.route('/admin/application/<int:app_id>/review', methods=['GET', 'POST'])
@login_required
def review_application(app_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash("Access denied.", "danger")
        return redirect(url_for('admin_dashboard'))

    application = LandApplication.query.get_or_404(app_id)
    documents = Document.query.filter_by(application_id=app_id).all()
    
    # Load conflicts with related application data
    conflicts = LandConflict.query.filter_by(application_id=app_id).order_by(LandConflict.created_at.desc()).all()
    conflicts_details = []
    try:
        for c in conflicts:
            parcel = None
            parcel_info = None
            conflicting_app = None
            
            if c.conflicting_parcel_id:
                try:
                    parcel = db.session.get(LandParcel, c.conflicting_parcel_id)
                    if parcel:
                        parcel_info = {
                            'id': parcel.id,
                            'parcel_number': parcel.parcel_number,
                            'owner_name': parcel.owner_name,
                        }
                        # Find the application associated with this parcel
                        if parcel.application_id:
                            conflicting_app = LandApplication.query.get(parcel.application_id)
                except Exception:
                    parcel = None
                    
            conflicts_details.append({
                'id': c.id,
                'title': c.title,
                'description': c.description,
                'conflict_type': c.conflict_type,
                'confidence_score': c.confidence_score,
                'overlap_percentage': c.overlap_percentage,
                'created_at': c.created_at,
                'parcel': parcel_info,
                'conflicting_application': conflicting_app
            })
    except Exception:
        conflicts_details = []
    
    # Get other recent applications (limit to 3)
    other_applications = LandApplication.query.filter(
        LandApplication.id != app_id,
        LandApplication.user_id.isnot(None)
    ).order_by(LandApplication.submitted_at.desc()).limit(3).all()

    if request.method == 'POST':
        admin_comment = request.form.get("admin_comment")
        if admin_comment:
            application.admin_comments = admin_comment
            db.session.commit()
            flash("Comment added successfully.", "success")
            return redirect(url_for('review_application', app_id=app_id))

    return render_template(
        'review_application.html',
        application=application,
        documents=documents,
        conflicts=conflicts_details,
        other_applications=other_applications
    )


@app.route('/admin/ai_training_data')
@login_required
def ai_training_data():
    if current_user.role != 'super_admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    all_docs = Document.query.all()
    training_data = []
    for doc in all_docs:
        text = extract_document_text(doc.file_path, doc.mime_type)
        training_data.append({
            'document_id': doc.id,
            'application_id': doc.application_id,
            'document_type': doc.document_type,
            'text': text
        })

    return render_template('ai_training_data.html', training_data=training_data)


@app.route('/admin/retrain_ai', methods=['POST'])
@login_required
def retrain_ai():
    if current_user.role != 'super_admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        import pickle

        all_docs = Document.query.all()
        all_texts = [extract_document_text(doc.file_path, doc.mime_type) for doc in all_docs]

        vectorizer = TfidfVectorizer(stop_words='english')
        vectorizer.fit(all_texts)

        with open('tfidf_vectorizer.pkl', 'wb') as f:
            pickle.dump(vectorizer, f)

        flash('AI model retrained successfully.', 'success')
    except Exception as e:
        flash(f'Error retraining AI model: {e}', 'danger')

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/application/<int:app_id>/ai_analysis')
@login_required
def ai_analysis(app_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    application = LandApplication.query.get_or_404(app_id)
    conflicts = LandConflict.query.filter_by(application_id=app_id).all()

    return render_template('ai_analysis.html', application=application, conflicts=conflicts)


@app.route('/admin/application/<int:app_id>/run_ai_analysis', methods=['POST', 'GET'])
@login_required
def run_ai_analysis(app_id):
    """Trigger AI conflict detection for a specific application.

    Admins only. This will create LandConflict rows and mark the application as processed.
    """
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    application = LandApplication.query.get_or_404(app_id)
    created = detect_conflicts(application.id)
    created.extend(detect_conflicts_from_documents(application.id))
    if created:
        flash(f'AI detected {len(created)} potential conflict(s).', 'warning')
    else:
        flash('AI analysis completed: no conflicts detected.', 'success')

    return redirect(url_for('review_application', app_id=app_id))


@app.route('/api/get_conflicts', methods=['GET'])
@login_required
def api_get_conflicts():
    """Return existing LandConflict rows for an application as JSON.

    Query params: application_id=int
    Returns: list of conflicts with optional parcel GeoJSON when available.
    """
    try:
        application_id = request.args.get('application_id', type=int)
        if not application_id:
            return jsonify([])

        conflicts = LandConflict.query.filter_by(application_id=application_id).all()
        out = []
        for c in conflicts:
            parcel = None
            try:
                parcel = db.session.get(LandParcel, c.conflicting_parcel_id) if c.conflicting_parcel_id else None
            except Exception:
                parcel = None

            parcel_geo = None
            if parcel and getattr(parcel, 'coordinates', None) is not None:
                try:
                    geom = to_shape(parcel.coordinates)
                    parcel_geo = geom.__geo_interface__
                except Exception:
                    parcel_geo = None

            out.append({
                'id': c.id,
                'title': c.title,
                'description': c.description,
                'confidence_score': c.confidence_score,
                'overlap_percentage': c.overlap_percentage,
                'conflict_type': c.conflict_type,
                'parcel': {
                    'id': parcel.id if parcel else None,
                    'parcel_number': parcel.parcel_number if parcel else None,
                    'geojson': parcel_geo
                }
            })

        return jsonify(out)
    except Exception:
        current_app.logger.exception('Failed to return conflicts')
        return jsonify([]), 500


@app.route('/api/geometry_conflicts', methods=['POST'])
@login_required
def api_geometry_conflicts():
    """Accepts { geometry: GeoJSON } and returns a list of parcels that intersect it.

    This is used by the registration page to check drawn geometries in real-time.
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'Invalid JSON'}), 400

    geom = payload.get('geometry')
    if not geom:
        return jsonify({'error': 'Missing geometry'}), 400

    try:
        # convert to shapely
        incoming = shape(geom)
    except Exception:
        return jsonify({'error': 'Invalid geometry'}), 400

    results = []
    try:
        parcels = LandParcel.query.filter(LandParcel.coordinates != None).all()
        for p in parcels:
            try:
                if p.coordinates is None:
                    continue
                parcel_geom = to_shape(p.coordinates)
                if parcel_geom.intersects(incoming):
                    inter = parcel_geom.intersection(incoming)
                    overlap_pct = None
                    try:
                        if parcel_geom.area > 0:
                            overlap_pct = float(inter.area / parcel_geom.area)
                    except Exception:
                        overlap_pct = None

                    results.append({
                        'parcel_id': p.id,
                        'parcel_number': p.parcel_number,
                        'owner_name': p.owner_name,
                        'overlap_pct': overlap_pct,
                        'geojson': parcel_geom.__geo_interface__
                    })
            except Exception:
                current_app.logger.exception('Error testing parcel geometry id=%s', getattr(p, 'id', None))

        return jsonify(results)
    except Exception:
        current_app.logger.exception('Geometry conflict check failed')
        return jsonify([]), 500


@app.route('/admin/conflict/<int:conflict_id>/resolve', methods=['POST'])
@login_required
def admin_resolve_conflict(conflict_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    conflict = LandConflict.query.get_or_404(conflict_id)
    resolved = resolve_conflict(conflict_id, resolved_by=current_user.id)
    if resolved:
        flash(f'Conflict #{conflict_id} marked as resolved.', 'success')
    else:
        flash('Failed to resolve conflict.', 'danger')

    # redirect back to the application review page if possible
    if conflict and conflict.application_id:
        return redirect(url_for('review_application', app_id=conflict.application_id))
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/conflict/<int:conflict_id>/notify', methods=['POST'])
@login_required
def admin_notify_conflict(conflict_id):
    """Admin action: compose & record (optionally send) a conflict notice to the applicant.

    The notice is stored in NotificationLog. If email/sms sending is configured the
    actual send can be wired later; for now we persist the message and mark it pending.
    """
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    conflict = LandConflict.query.get_or_404(conflict_id)
    application = None
    if conflict.application_id:
        application = db.session.get(LandApplication, conflict.application_id)

    if not application:
        flash('Related application not found for this conflict.', 'danger')
        return redirect(url_for('admin_dashboard'))

    recipient = application.email or application.phone_number or 'unknown'
    # Allow override from form (preview modal) if provided
    subject = request.form.get('subject') or f"Conflict detected for application {application.reference_number}"
    # Build a helpful message body including conflict details
    body_lines = [
        f"Dear {application.applicant_name},",
        "",
        "We have detected a potential conflict related to your land application. Below are the details:",
        "",
        f"Application ref: {application.reference_number}",
        f"Conflict: {conflict.title}",
        f"Type: {conflict.conflict_type}",
        f"Confidence: {round((conflict.confidence_score or 0)*100, 1)}%",
    ]
    if conflict.overlap_percentage is not None:
        body_lines.append(f"Overlap: {round(conflict.overlap_percentage * 100, 2)}%")
    if conflict.description:
        body_lines.extend(["", "Details:", conflict.description])

    body_lines.extend(["", "Suggested actions:", "- Please review your uploaded documents.", "- Provide clarifying documents if available.", "- Contact the registry if you believe this is an error.", "", "Regards,", SystemSettings.get_setting('system_name', 'Ndola Land Registry')])

    message_body = request.form.get('message') or "\n".join(body_lines)

    try:
        n = NotificationLog(
            user_id=current_user.id,
            application_id=application.id,
            notification_type='email',
            recipient=recipient,
            subject=subject,
            message=message_body,
            status='pending'
        )
        db.session.add(n)
        db.session.commit()
        log_audit('send_conflict_notice', 'notification_logs', n.id, None, {'recipient': recipient, 'conflict_id': conflict_id})
        flash('Conflict notice has been recorded and queued for sending.', 'success')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to create notification log')
        flash('Failed to queue conflict notice. See logs.', 'danger')

    return redirect(url_for('review_application', app_id=application.id))


def _send_smtp_email(recipient: str, subject: str, body: str):
    """Send an email using SMTP configured via environment variables.
    Returns (True, None) on success or (False, error_str) on failure.
    """
    host = os.environ.get('SMTP_HOST')
    port = int(os.environ.get('SMTP_PORT', '0')) if os.environ.get('SMTP_PORT') else None
    user = os.environ.get('SMTP_USER')
    password = os.environ.get('SMTP_PASSWORD')
    from_addr = os.environ.get('SMTP_FROM') or user
    use_tls = os.environ.get('SMTP_USE_TLS', 'true').lower() in ('1','true','yes')

    if not host or not from_addr:
        return False, 'SMTP not configured (SMTP_HOST/SMTP_FROM required)'

    msg = MIMEMultipart()
    msg['From'] = from_addr
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    try:
        if use_tls:
            server = smtplib.SMTP(host, port or 587, timeout=20)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP(host, port or 25, timeout=20)

        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, [recipient], msg.as_string())
        server.quit()
        return True, None
    except Exception as e:
        app.logger.exception('SMTP send failed')
        return False, str(e)


def _notification_worker_loop(sleep_seconds=10):
    """Background worker that sends pending NotificationLog entries."""
    with app.app_context():
        while True:
            try:
                pending = (
                    db.session.query(NotificationLog)
                    .filter(NotificationLog.status == 'pending')
                    .order_by(NotificationLog.id.asc())
                    .limit(10)
                    .all()
                )
                if not pending:
                    time.sleep(sleep_seconds)
                    continue

                for note in pending:
                    try:
                        ok, err = _send_smtp_email(note.recipient, note.subject, note.message)
                        if ok:
                            note.status = 'sent'
                            note.sent_at = datetime.utcnow()
                            db.session.add(note)
                            db.session.commit()
                            app.logger.info(f"Notification sent: {note.id} to {note.recipient}")
                        else:
                            note.status = 'failed'
                            db.session.add(note)
                            db.session.commit()
                            app.logger.error(f"Notification {note.id} failed: {err}")
                    except Exception:
                        app.logger.exception('Error sending notification')
                        db.session.rollback()
                time.sleep(1)
            except Exception:
                app.logger.exception('Notification worker crashed; sleeping before retry')
                time.sleep(sleep_seconds)


def start_notification_worker():
    try:
        t = threading.Thread(target=_notification_worker_loop, args=(10,), daemon=True)
        t.start()
        app.logger.info('Notification worker started')
    except Exception:
        app.logger.exception('Failed to start notification worker')


# Preview route for composing messages (AJAX)
@app.route('/admin/conflict/<int:conflict_id>/preview')
@login_required
def admin_conflict_preview(conflict_id):
    conflict = db.session.get(LandConflict, conflict_id)
    if not conflict:
        return jsonify({'error': 'Conflict not found'}), 404
    application = db.session.get(LandApplication, conflict.application_id)
    if not application:
        return jsonify({'error': 'Application not found'}), 404
    subject = f"Conflict detected for application {application.reference_number}"
    message = (
        f"Dear {application.applicant_name},\n\n"
        f"Our automated checks found a potential conflict (ID: {conflict.id}) on your application.\n\n"
        f"Conflict summary:\n{conflict.title}\n{conflict.description}\n\n"
        "Please log in to review the details and contact the registry if you believe this is an error.\n\n"
        "Regards,\nRegistry Team"
    )
    return jsonify({'subject': subject, 'message': message, 'recipient': application.email or application.phone_number})


@app.route('/admin/notifications')
@login_required
def admin_notifications():
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    q = db.session.query(NotificationLog).order_by(NotificationLog.created_at.desc())
    total = q.count()
    items = q.offset((page-1)*per_page).limit(per_page).all()
    return render_template('admin_notifications.html', items=items, page=page, per_page=per_page, total=total)


@app.route('/admin/notifications/<int:note_id>/resend', methods=['POST'])
@login_required
def admin_notifications_resend(note_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    note = db.session.get(NotificationLog, note_id)
    if not note:
        flash('Notification not found', 'danger')
        return redirect(url_for('admin_notifications'))
    note.status = 'pending'
    note.sent_at = None
    db.session.add(note)
    db.session.commit()
    flash('Notification re-queued for sending', 'success')
    return redirect(url_for('admin_notifications'))


# Login verification flow handled by login() and /login/verify





from flask import send_from_directory, abort

from flask import send_from_directory, abort

@app.route('/uploads/<int:user_id>/<int:app_id>/<path:filename>')
@login_required
def view_document(user_id, app_id, filename):

    if current_user.role not in ['admin', 'super_admin'] and current_user.id != user_id:
        abort(403)

    try:
        # Build the file path
        user_dir = f"user_{user_id}"
        app_dir = f"app_{app_id}"
        
        # Return the file
        return send_from_directory(
            os.path.join(app.config['UPLOAD_FOLDER'], user_dir, app_dir),
            filename,
            as_attachment=False  # View in browser, not download
        )
    except FileNotFoundError:
        current_app.logger.error(f"File not found: {filename}")
        abort(404)
    except Exception as e:
        current_app.logger.error(f"Error serving document: {e}")
        abort(500)


@app.route('/add_comment/<int:app_id>', methods=['POST'])
@login_required
def add_comment(app_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash("You don’t have permission to comment on applications.", "danger")
        return redirect(url_for('admin_dashboard'))

    application = LandApplication.query.get_or_404(app_id)
    comment = request.form.get('comment')

    if comment:
        application.admin_comments = comment
        application.reviewed_by = current_user.id
        application.reviewed_at = datetime.utcnow()
        db.session.commit()
        flash("Comment added successfully.", "success")
    else:
        flash("Comment cannot be empty.", "danger")

    return redirect(url_for('admin_dashboard'))



@app.route("/approve_application/<int:app_id>", methods=["POST", "GET"])
@login_required
def approve_application(app_id):
    if current_user.role not in ["admin", "super_admin"]:
        flash("You do not have permission to approve applications.", "danger")
        return redirect(url_for("admin_dashboard"))

    application = LandApplication.query.get_or_404(app_id)

    old_status = application.status
    application.status = "approved"
    application.reviewed_by = current_user.id
    application.reviewed_at = datetime.utcnow()

    #  Update related documents
    for doc in application.documents:
        doc.status = "approved"

    db.session.commit()

    log_audit(
        "approve_application", "land_applications", app_id,
        {"status": old_status},
        {"status": "approved"}
    )

    flash(f"Application #{app_id} approved successfully!", "success")
    return redirect(url_for("admin_dashboard"))


@app.route('/admin/application/<int:app_id>/approve_payment', methods=['POST'])
@login_required
def approve_payment(app_id):
    """Approve payment for an application."""
    if current_user.role not in ['admin', 'super_admin']:
        flash('You do not have permission to approve payments.', 'danger')
        return redirect(url_for('admin_dashboard'))

    application = LandApplication.query.get_or_404(app_id)
    
    old_payment_status = application.payment_status
    application.payment_status = 'paid'
    
    db.session.commit()
    
    log_audit(
        'approve_payment', 'land_applications', app_id,
        {'payment_status': old_payment_status},
        {'payment_status': 'paid'}
    )
    
    flash(f'Payment for application #{application.reference_number} has been approved!', 'success')
    return redirect(url_for('review_application', app_id=app_id))









@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration with role selection."""
    if request.method == 'POST':
        try:
            # Get form data
            username = request.form.get('username')
            email = request.form.get('email')
            password = request.form.get('password')
            first_name = request.form.get('first_name')
            last_name = request.form.get('last_name')
            phone_number = request.form.get('phone_number')
            account_type = request.form.get('account_type', 'citizen')
            
            # Validate account type
            if account_type not in ['citizen', 'seller']:
                flash('Invalid account type.', 'danger')
                return redirect(url_for('register'))
            
            # Check existing username
            if User.query.filter_by(username=username).first():
                flash('Username already exists.', 'danger')
                return redirect(url_for('register'))
            
            # Check existing email
            if User.query.filter_by(email=email).first():
                flash('Email already registered.', 'danger')
                return redirect(url_for('register'))
            
            # Create user with selected role
            new_user = User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone_number=phone_number,
                role=account_type,  # CRITICAL: Set role from form
                is_active=True
            )
            new_user.set_password(password)
            
            db.session.add(new_user)
            db.session.commit()
            
            # Log registration
            log_audit('user_registration', 'users', new_user.id, None, {
                'username': username,
                'role': account_type
            })
            
            # Auto-login
            login_user(new_user)
            
            # CRITICAL: Role-based redirect
            if account_type == 'seller':
                flash(f'Welcome {first_name}! Seller account created.', 'success')
                return redirect(url_for('seller_dashboard'))
            else:  # citizen
                flash(f'Welcome {first_name}! Account created.', 'success')
                return redirect(url_for('index'))
        
        except Exception as e:
            db.session.rollback()
            flash(f'Registration error: {str(e)}', 'danger')
    
    return render_template('register.html')








# this route has been changed in case it gets messed up replace it 
@app.route('/register_land', methods=['GET', 'POST'])
@login_required

def register_land():
    """Handle land registration for citizens - FIXED VERSION with form data preservation"""
    if current_user.role != "citizen":
        flash("Only citizens can register land", "danger")
        return redirect(url_for("index"))

    if request.method == 'POST':
        try:
            # Generate reference number
            reference_number = f"LR-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

            # Get registration type and fees
            registration_type = request.form.get('registration_type')
            if not registration_type:
                flash('Please select a registration type', 'danger')
                return render_template('register_land.html', form_data=request.form)

            try:
                payment_amount = float(request.form.get('payment_amount', 0))
            except Exception:
                payment_amount = 0.0

            declared_value_raw = request.form.get('declared_value', '')
            declared_value = 0.0
            try:
                if declared_value_raw and declared_value_raw.strip() != '':
                    declared_value = float(declared_value_raw.replace(',',''))
            except Exception:
                flash('Declared value must be a valid number', 'danger')
                return render_template('register_land.html', form_data=request.form)

            # Document requirements per type
            doc_requirements = {
                'transfer': ['seller_id','buyer_id','seller_tpin','buyer_tpin','sale_agreement','current_title_deed'],
                'lease': ['offer_letter','lease_agreement','survey_map','proof_rent','nrc_copy','tpin_certificate'],
                'mortgage': ['mortgage_deed','lender_name_tpin','borrower_id','secured_amount'],
                'title_issue': ['offer_letter','survey_map','nrc_copy','tpin_certificate'],
                'subdivision': ['original_title_copy','survey_map','nrc_copy','application_letter'],
                'replacement': ['police_report','statutory_declaration','nrc_copy'],
                'change_ownership': ['assignment_deed','old_title_copy','seller_tpin','buyer_tpin','nrc_copy'],
                'caveat': ['caveat_document','nrc_copy','proof_of_interest']
            }

            required_keys = doc_requirements.get(registration_type, [])

            # Validate declared value for specific types
            needs_declared = registration_type in ('transfer','mortgage','change_ownership')
            if needs_declared and (not declared_value or declared_value <= 0):
                flash('Declared property value is required for this registration type', 'danger')
                return render_template('register_land.html', form_data=request.form)

            # Validate NRC
            nrc_number = (request.form.get('nrc_number') or '').strip()
            nrc_valid, nrc_error = validate_nrc(nrc_number)
            if not nrc_valid:
                # Try passport validation
                from validation_utils import validate_passport
                passport_valid, passport_error = validate_passport(nrc_number)
                if not passport_valid:
                    flash(f'Invalid ID: {nrc_error}', 'danger')
                    return render_template('register_land.html', form_data=request.form)

            # Validate TPIN if required
            tpin_val = (request.form.get('tpin_number') or '').strip()
            tpin_required_types = ['transfer','mortgage','change_ownership','title_issue']
            if registration_type in tpin_required_types:
                tpin_valid, tpin_error = validate_tpin(tpin_val)
                if not tpin_valid:
                    flash(f'Invalid TPIN: {tpin_error}', 'danger')
                    return render_template('register_land.html', form_data=request.form)
            
            # Validate phone
            phone_number = request.form.get('phone_number')
            if phone_number:
                phone_valid, phone_error = validate_phone(phone_number)
                if not phone_valid:
                    flash(f'Invalid phone: {phone_error}', 'danger')
                    return render_template('register_land.html', form_data=request.form)
            
            # Validate email
            email = request.form.get('email')
            if email:
                email_valid, email_error = validate_email(email)
                if not email_valid:
                    flash(f'Invalid email: {email_error}', 'danger')
                    return render_template('register_land.html', form_data=request.form)
            
            # Check for identity duplicates BEFORE creating application
            identity_dups = check_identity_duplicate(nrc_number, tpin_val)
            if identity_dups:
                dup_refs = ', '.join([app.reference_number for app in identity_dups[:3]])
                flash(f'Warning: Your NRC/TPIN matches existing application(s): {dup_refs}. ' +
                      'You may have already applied. Contact registry if this is an error.', 'warning')
                # Don't block, but warn user

            # Check for missing required documents
            missing = []
            for key in required_keys:
                if key == 'secured_amount':
                    if not request.form.get('secured_amount'):
                        missing.append('Secured Amount')
                    continue
                f = request.files.get(key)
                if not f or f.filename == '':
                    missing.append(key.replace('_', ' ').title())
            
            if missing:
                flash(f'Missing required documents: {", ".join(missing[:3])}{"..." if len(missing) > 3 else ""}', 'danger')
                return render_template('register_land.html', form_data=request.form)

            # Get and validate geometry
            geometry_json = request.form.get('land_geometry')
            if not geometry_json:
                flash('Please draw your land parcel on the map', 'danger')
                return render_template('register_land.html', form_data=request.form)

            # Parse geometry
            try:
                geom_dict = json.loads(geometry_json)
                geom = shape(geom_dict)
                geom_wkb = from_shape(geom, srid=4326)
            except Exception as e:
                current_app.logger.exception('Failed to parse geometry')
                flash('Invalid map geometry. Please redraw your parcel', 'danger')
                return render_template('register_land.html', form_data=request.form)

            # Create application
            application = LandApplication(
                reference_number=reference_number,
                applicant_name=request.form.get('applicant_name'),
                nrc_number=request.form.get('nrc_number'),
                tpin_number=request.form.get('tpin_number'),
                phone_number=request.form.get('phone_number'),
                email=request.form.get('email'),
                land_location=request.form.get('land_location'),
                land_size=float(request.form.get('land_size', 0)),
                land_use=request.form.get('land_use'),
                land_description=request.form.get('land_description'),
                declared_value=declared_value,
                status="pending",
                priority="medium",
                ai_conflict_score=0.0,
                ai_duplicate_score=0.0,
                ai_processed=False,
                processing_fee=payment_amount,
                payment_status="pending",
                user_id=current_user.id,
                registration_type=registration_type,
                coordinates=geom_wkb
            )
            db.session.add(application)
            db.session.flush()

            # Create parcel
            parcel = LandParcel(
                parcel_number=f"PN-{application.id}",
                owner_name=application.applicant_name,
                owner_nrc=application.nrc_number,
                owner_phone=application.phone_number,
                owner_email=application.email,
                size=application.land_size,
                location=application.land_location,
                land_use=application.land_use,
                land_description=application.land_description,
                status="registered",
                application_id=application.id,
                coordinates=geom_wkb
            )
            db.session.add(parcel)

            # Document type labels
            label_map = {
                'nrc_copy': 'NRC Copy',
                'tpin_certificate': 'TPIN Certificate',
                'survey_map': 'Survey Map',
                'allocation_letter': 'Allocation Letter',
                'current_title_deed': 'Current Title Deed',
                'seller_id': 'Seller ID',
                'buyer_id': 'Buyer ID',
                'seller_tpin': 'Seller TPIN',
                'buyer_tpin': 'Buyer TPIN',
                'sale_agreement': 'Sale Agreement',
                'offer_letter': 'Offer Letter',
                'lease_agreement': 'Lease Agreement',
                'proof_rent': 'Proof of Rent',
                'mortgage_deed': 'Mortgage Deed',
                'lender_name_tpin': 'Lender Name & TPIN',
                'borrower_id': 'Borrower ID',
                'original_title_copy': 'Original Title Copy',
                'application_letter': 'Application Letter',
                'police_report': 'Police Report',
                'statutory_declaration': 'Statutory Declaration',
                'assignment_deed': 'Assignment Deed',
                'old_title_copy': 'Old Title Copy',
                'caveat_document': 'Caveat Document',
                'proof_of_interest': 'Proof of Interest'
            }

            # Save required files
            for key in required_keys:
                if key == 'secured_amount':
                    continue
                fileobj = request.files.get(key)
                if fileobj and fileobj.filename != "":
                    try:
                        saved = _save_upload(fileobj, current_user.id, application.id, key)
                    except ValueError as ve:
                        db.session.rollback()
                        flash(str(ve), 'danger')
                        return render_template('register_land.html', form_data=request.form)
                    
                    filepath = os.path.join(
                        current_app.config['UPLOAD_FOLDER'],
                        f"user_{current_user.id}",
                        f"app_{application.id}",
                        saved
                    )
                    
                    doc = Document(
                        application_id=application.id,
                        document_type=label_map.get(key, key.replace('_', ' ').title()),
                        filename=saved,
                        original_filename=fileobj.filename,
                        file_path=filepath,
                        file_size=os.path.getsize(filepath),
                        mime_type=fileobj.mimetype,
                        file_hash=hashlib.sha256(open(filepath, 'rb').read()).hexdigest()
                    )
                    db.session.add(doc)

            # Save additional documents
            additional_files = request.files.getlist('additional_docs') or []
            for idx, fileobj in enumerate(additional_files):
                if fileobj and fileobj.filename != "":
                    saved = _save_upload(fileobj, current_user.id, application.id, f"additional_{idx}")
                    filepath = os.path.join(
                        current_app.config['UPLOAD_FOLDER'],
                        f"user_{current_user.id}",
                        f"app_{application.id}",
                        saved
                    )
                    doc = Document(
                        application_id=application.id,
                        document_type='Additional Document',
                        filename=saved,
                        original_filename=fileobj.filename,
                        file_path=filepath,
                        file_size=os.path.getsize(filepath),
                        mime_type=fileobj.mimetype,
                        file_hash=hashlib.sha256(open(filepath, 'rb').read()).hexdigest()
                    )
                    db.session.add(doc)

            # Save secured amount if provided
            secured_amt_raw = request.form.get('secured_amount')
            if secured_amt_raw:
                try:
                    application.secured_amount = float(secured_amt_raw)
                except Exception:
                    application.secured_amount = None

            # Save annual rent if provided
            annual_rent_raw = request.form.get('annual_rent')
            if annual_rent_raw:
                try:
                    application.annual_rent = float(annual_rent_raw)
                except Exception:
                    application.annual_rent = None

            # Commit all changes
            db.session.commit()

            # START COMPREHENSIVE DUPLICATE DETECTION
            try:
                def _bg_detect(aid):
                    with app.app_context():
                        try:
                            from ai_conflict import detect_conflicts
                            from ai_conflict_enhanced import detect_conflicts_from_documents
                            from duplicate_detector import detect_all_duplicates
                            
                            print(f"[BG DETECTION] Starting for app {aid}")
                            spatial_conflicts = detect_conflicts(aid)
                            print(f"[BG DETECTION] Spatial: {len(spatial_conflicts)} conflicts")
                            doc_conflicts = detect_conflicts_from_documents(aid)
                            print(f"[BG DETECTION] Documents: {len(doc_conflicts)} conflicts")
                            dup_conflicts = detect_all_duplicates(aid)
                            print(f"[BG DETECTION] Duplicates: {len(dup_conflicts)} conflicts")
                            print(f"[BG DETECTION] Complete for app {aid}")
                        except Exception as e:
                            print(f"[BG DETECTION] ERROR: {e}")
                            import traceback
                            traceback.print_exc()

                import threading
                thread = threading.Thread(target=_bg_detect, args=(application.id,), daemon=True)
                thread.start()
                print(f"[MAIN] Background detection thread started for app {application.id}")
            except Exception as e:
                current_app.logger.exception('Failed to start duplicate detection thread')

            # Log the audit
            log_audit('create_application', 'land_applications', application.id, None, {
                'reference': application.reference_number,
                'type': registration_type
            })
            
            # Success message
            flash(f'Land application submitted successfully! Reference: {application.reference_number}', 'success')
            return redirect(url_for('application_status'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Application submission failed')
            flash(f'Application failed: {str(e)}', 'danger')
            return render_template('register_land.html', form_data=request.form)
    
    # GET request - show the form
    return render_template('register_land.html')






@app.route('/application/<int:app_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_application(app_id):
    """Handle editing of an existing land application."""
    application = LandApplication.query.get_or_404(app_id)

    # Security check: ensure the current user owns this application
    if application.user_id != current_user.id:
        flash("You do not have permission to edit this application.", "danger")
        return redirect(url_for('application_status'))

    # Prevent editing of applications that are already approved
    if application.status == 'approved':
        flash("Approved applications cannot be edited.", "info")
        return redirect(url_for('application_status'))

    if request.method == 'POST':
        try:
            # Update main application fields
            application.applicant_name = request.form.get('applicant_name')
            application.nrc_number = request.form.get('nrc_number')
            application.tpin_number = request.form.get('tpin_number')
            application.phone_number = request.form.get('phone_number')
            application.email = request.form.get('email')
            application.land_location = request.form.get('land_location')
            application.land_use = request.form.get('land_use')
            application.land_description = request.form.get('land_description')
            
            # Reset status to pending since it's being re-submitted
            application.status = 'pending'
            application.reviewed_by = None
            application.reviewed_at = None
            application.admin_comments = None
            application.rejection_reason = None

            # Update geometry if it has changed
            geometry_json = request.form.get('land_geometry')
            if geometry_json:
                try:
                    geom_dict = json.loads(geometry_json)
                    geom = shape(geom_dict)
                    application.coordinates = from_shape(geom, srid=4326)
                    application.land_size = float(request.form.get('land_size', 0))
                except Exception as e:
                    current_app.logger.error(f"Could not update geometry for app {app_id}: {e}")
            
            db.session.commit()
            
            flash('Application updated successfully. It is now pending review again.', 'success')
            return redirect(url_for('application_status'))

        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Application edit failed')
            flash(f"Update failed: {str(e)}", "danger")
            return render_template("register_land.html", application=application, edit_mode=True)

    # For a GET request, show the form with the existing data
    return render_template("register_land.html", application=application, edit_mode=True)








@app.route('/about')
def about():
    """Renders the about page."""
    return render_template('about.html')


@app.route('/application_status')
@login_required
def application_status():
    """Renders the application status page for the current user."""
    user_applications = LandApplication.query.filter_by(user_id=current_user.id).all()
    return render_template('application_status.html', applications=user_applications)


@app.route('/api/check-auth')
def check_auth():
    """API endpoint to check user authentication status."""
    if current_user.is_authenticated:
        return jsonify({
            'authenticated': True,
            'username': current_user.username,
            'role': current_user.role,
            'user_id': current_user.id
        })
    else:
        return jsonify({
            'authenticated': False
        })


@app.route('/api/get_conflict_applications', methods=['POST'])
@login_required
def get_conflict_applications():
    """Get detailed information about conflicting applications."""
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json()
        conflicts = data.get('conflicts', [])
        
        if not conflicts:
            return jsonify({'applications': []})
        
        # Extract conflicting application IDs
        app_ids = [c.get('conflicting_app_id') for c in conflicts if c.get('conflicting_app_id')]
        
        if not app_ids:
            return jsonify({'applications': []})
        
        # Fetch the applications
        applications = LandApplication.query.filter(LandApplication.id.in_(app_ids)).all()
        
        result = []
        for app in applications:
            result.append({
                'id': app.id,
                'reference_number': app.reference_number,
                'applicant_name': app.applicant_name,
                'land_location': app.land_location,
                'status': app.status,
                'submitted_at': app.submitted_at.strftime('%Y-%m-%d %H:%M')
            })
        
        return jsonify({'applications': result})
    except Exception as e:
        current_app.logger.exception('Error fetching conflict applications')
        return jsonify({'error': str(e)}), 500


@app.route('/api/get_application_geometry/<int:app_id>')
@login_required
def get_application_geometry(app_id):
    """Get geometry data for an application and its conflicts for map display."""
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        application = LandApplication.query.get_or_404(app_id)
        
        result = {
            'geometry': None,
            'conflicts': []
        }
        
        # Get application geometry
        if application.coordinates:
            try:
                geom = to_shape(application.coordinates)
                result['geometry'] = geom.__geo_interface__
            except Exception as e:
                current_app.logger.error(f'Failed to convert application geometry: {e}')
        
        # Get conflicting parcel geometries
        conflicts = LandConflict.query.filter_by(application_id=app_id).all()
        for conflict in conflicts:
            if conflict.conflicting_parcel_id:
                try:
                    parcel = db.session.get(LandParcel, conflict.conflicting_parcel_id)
                    if parcel and parcel.coordinates:
                        parcel_geom = to_shape(parcel.coordinates)
                        result['conflicts'].append({
                            'parcel_number': parcel.parcel_number,
                            'owner_name': parcel.owner_name,
                            'geojson': parcel_geom.__geo_interface__
                        })
                except Exception as e:
                    current_app.logger.error(f'Failed to convert parcel geometry: {e}')
        
        return jsonify(result)
    except Exception as e:
        current_app.logger.exception('Error fetching application geometry')
        return jsonify({'error': str(e)}), 500


@app.route("/reject_application/<int:app_id>", methods=["POST", "GET"])
@login_required
def reject_application(app_id):
    if current_user.role not in ["admin", "super_admin"]:
        flash("You do not have permission to reject applications.", "danger")
        return redirect(url_for("admin_dashboard"))

    application = LandApplication.query.get_or_404(app_id)

    old_status = application.status
    application.status = "rejected"
    application.reviewed_by = current_user.id
    application.reviewed_at = datetime.utcnow()
    application.rejection_reason = request.form.get("reason", "Not specified")

    #  Update related documents
    for doc in application.documents:
        doc.status = "rejected"

    db.session.commit()

    log_audit(
        "reject_application", "land_applications", app_id,
        {"status": old_status},
        {"status": "rejected", "reason": application.rejection_reason}
    )

    flash(f"Application #{app_id} rejected.", "danger")
    return redirect(url_for("admin_dashboard"))





@app.route("/generate_report")
@login_required
def generate_report():
    """Generates a PDF report of all land applications."""
    # Restrict access to admins and super admins
    if current_user.role not in ["admin", "super_admin"]:
        flash("You do not have permission to generate reports.", "danger")
        return redirect(url_for("admin_dashboard"))

    # Fetch all applications from the database
    applications = LandApplication.query.filter(LandApplication.user_id.isnot(None)).all()

    
    # Create an in-memory buffer to hold the PDF
    buffer = io.BytesIO()
    
    # Create the PDF document
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    
    # A list to hold the flowable objects (Paragraphs, Tables, etc.)
    story = []

    # Get sample styles
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Centered', alignment=TA_CENTER))

    # Add a title
    title = Paragraph("Ndola Land Registry System - Application Report", styles['Centered'])
    story.append(title)
    story.append(Spacer(1, 0.2 * inch))

    # Add a summary section
    total_count = len(applications)
    pending_count = sum(1 for app in applications if app.status == 'pending')
    approved_count = sum(1 for app in applications if app.status == 'approved')
    rejected_count = sum(1 for app in applications if app.status == 'rejected')
    conflict_count = sum(1 for app in applications if app.status == 'conflict')
    
    summary_text = f"Total Applications: {total_count}<br/>"
    summary_text += f"<font color='blue'>Pending: {pending_count}</font><br/>"
    summary_text += f"<font color='green'>Approved: {approved_count}</font><br/>"
    summary_text += f"<font color='red'>Rejected: {rejected_count}</font><br/>"
    summary_text += f"<font color='orange'>Conflicts: {conflict_count}</font>"
    
    summary_paragraph = Paragraph(summary_text, styles['Normal'])
    story.append(summary_paragraph)
    story.append(Spacer(1, 0.2 * inch))
    
    # Create the table data
    data = [['Reference #', 'Applicant Name', 'Land Location', 'Status', 'Date Submitted']]
    for app in applications:
        status_color = black
        if app.status == 'approved':
            status_color = green
        elif app.status == 'rejected':
            status_color = red
        elif app.status == 'conflict':
            status_color = colors.orange

        row_data = [
            Paragraph(str(app.reference_number), styles['Normal']),
            Paragraph(app.applicant_name, styles['Normal']),
            Paragraph(app.land_location, styles['Normal']),
            Paragraph(app.status.capitalize(), ParagraphStyle(name='StatusStyle', textColor=status_color, fontName='Helvetica-Bold')),
            Paragraph(app.submitted_at.strftime('%Y-%m-%d'), styles['Normal'])
        ]
        data.append(row_data)

    # Style the table
    table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#007BFF')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
    ])

    # Create the table object and add it to the story
    report_table = Table(data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 0.8*inch, 1.2*inch])
    report_table.setStyle(table_style)
    story.append(report_table)

    # Build the document
    doc.build(story)
    
    # Get the value of the buffer and send it as a file
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name='land_registry_report.pdf', mimetype='application/pdf')

# --- Seller Routes ---
@app.route('/seller/dashboard')
@login_required
def seller_dashboard():
    """Seller dashboard showing their listings and stats."""
    if current_user.role not in ['citizen', 'seller']:
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for("index"))
    
    # Get all listings for current user
    listings = AvailableLand.query.filter_by(seller_id=current_user.id).all()
    
    # Calculate stats
    total_listings = len(listings)
    active_listings = len([l for l in listings if l.status == 'active'])
    sold_listings = len([l for l in listings if l.status == 'sold'])
    pending_listings = len([l for l in listings if l.admin_approval_status == 'pending'])
    total_views = sum(l.view_count for l in listings)
    
    stats = {
        'total_listings': total_listings,
        'active_listings': active_listings,
        'sold_listings': sold_listings,
        'pending_listings': pending_listings,
        'total_views': total_views
    }
    
    return render_template('seller/dashboard.html', 
                          listings=listings, 
                          stats=stats)


@app.route('/seller/post_land', methods=['GET', 'POST'])
@login_required
def post_land():
    """Allow sellers to post new land listings."""
    # Only users with role 'seller' may post marketplace listings
    if current_user.role != 'seller':
        flash("Only sellers can post land listings.", "danger")
        return redirect(url_for("index"))
    
    if request.method == 'POST':
        try:
            # Generate listing reference
            listing_ref = f"AL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            
            # Get form data
            title = request.form.get('title')
            description = request.form.get('description')
            location = request.form.get('location')
            size = float(request.form.get('size', 0))
            land_use = request.form.get('land_use')
            asking_price = float(request.form.get('asking_price', 0))
            property_type = request.form.get('property_type')
            is_registered = request.form.get('is_registered') == 'yes'
            seller_phone = request.form.get('seller_phone')
            seller_email = request.form.get('seller_email')
            seller_whatsapp = request.form.get('seller_whatsapp', '')
            
            # Validate required fields
            if not all([title, description, location, size, land_use, asking_price, property_type, seller_phone, seller_email]):
                flash('All required fields must be filled.', 'danger')
                return render_template('seller/post_land.html')
            
            # Handle coordinates (optional but recommended)
            coordinates_wkb = None
            coordinates_json = request.form.get('coordinates')
            if coordinates_json:
                try:
                    geom_dict = json.loads(coordinates_json)
                    geom = shape(geom_dict)
                    coordinates_wkb = from_shape(geom, srid=4326)
                except Exception as e:
                    current_app.logger.warning(f'Could not parse coordinates: {e}')
            
            # Collect amenities
            amenities = []
            if request.form.get('water'):
                amenities.append('water')
            if request.form.get('electricity'):
                amenities.append('electricity')
            if request.form.get('road_access'):
                amenities.append('road_access')
            if request.form.get('fence'):
                amenities.append('fence')
            if request.form.get('title_deed'):
                amenities.append('title_deed')
            
            # Handle image uploads
            images = []
            property_images = request.files.getlist('property_images') or []
            for idx, fileobj in enumerate(property_images[:10]):  # Max 10 images
                if fileobj and fileobj.filename != '':
                    try:
                        # Create directory for listing images
                        listing_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'listings', f'listing_{listing_ref}')
                        os.makedirs(listing_dir, exist_ok=True)
                        
                        fname = secure_filename(fileobj.filename)
                        timestamp = int(time.time())
                        random_token = secrets.token_hex(6)
                        unique_filename = f"image-{timestamp}-{random_token}-{fname}"
                        
                        filepath = os.path.join(listing_dir, unique_filename)
                        fileobj.save(filepath)
                        
                        images.append(unique_filename)
                    except Exception as e:
                        current_app.logger.error(f'Error saving image: {e}')
            
            # Handle supporting documents (admin only)
            documents = []
            supporting_docs = request.files.getlist('supporting_documents') or []
            for idx, fileobj in enumerate(supporting_docs):
                if fileobj and fileobj.filename != '':
                    try:
                        doc_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'listings', f'listing_{listing_ref}', 'docs')
                        os.makedirs(doc_dir, exist_ok=True)
                        
                        fname = secure_filename(fileobj.filename)
                        timestamp = int(time.time())
                        random_token = secrets.token_hex(6)
                        unique_filename = f"doc-{timestamp}-{random_token}-{fname}"
                        
                        filepath = os.path.join(doc_dir, unique_filename)
                        fileobj.save(filepath)
                        
                        documents.append(unique_filename)
                    except Exception as e:
                        current_app.logger.error(f'Error saving document: {e}')
            
            # Create AvailableLand record
            available_land = AvailableLand(
                listing_reference=listing_ref,
                title=title,
                description=description,
                location=location,
                size=size,
                land_use=land_use,
                asking_price=asking_price,
                property_type=property_type,
                is_registered=is_registered,
                coordinates=coordinates_wkb,
                seller_id=current_user.id,
                seller_name=f"{current_user.first_name} {current_user.last_name}".strip() or current_user.username,
                seller_phone=seller_phone,
                seller_email=seller_email,
                seller_whatsapp=seller_whatsapp,
                amenities=amenities if amenities else None,
                images=images if images else None,
                documents=documents if documents else None,
                # FIXED: Seller listings should be pending admin approval before showing publicly
                status='pending',
                admin_approval_status='pending',
                approved_at=None
            )
            
            db.session.add(available_land)
            db.session.commit()
            
            log_audit('create_land_listing', 'available_lands', available_land.id, None, {
                'title': title,
                'reference': listing_ref
            })
            
            flash(f'Land listing submitted successfully! Reference: {listing_ref}. Your listing is pending admin approval.', 'success')
            return redirect(url_for('seller_dashboard'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Error creating land listing')
            flash(f'Error creating listing: {str(e)}', 'danger')
            return render_template('seller/post_land.html')
    
    # GET request - show the form
    return render_template('seller/post_land.html')


@app.route('/seller/listing/<int:listing_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_listing(listing_id):
    """Edit an existing land listing."""
    if current_user.role != 'seller':
        flash('Only sellers can edit listings.', 'danger')
        return redirect(url_for('index'))
    
    listing = AvailableLand.query.get_or_404(listing_id)
    
    # Security check: ensure the current user owns this listing
    if listing.seller_id != current_user.id:
        flash('You do not have permission to edit this listing.', 'danger')
        return redirect(url_for('seller_dashboard'))
    
    if request.method == 'POST':
        try:
            # Update listing fields
            listing.title = request.form.get('title')
            listing.description = request.form.get('description')
            listing.location = request.form.get('location')
            listing.size = float(request.form.get('size', 0))
            listing.land_use = request.form.get('land_use')
            listing.asking_price = float(request.form.get('asking_price', 0))
            listing.property_type = request.form.get('property_type')
            listing.is_registered = request.form.get('is_registered') == 'yes'
            listing.seller_phone = request.form.get('seller_phone')
            listing.seller_email = request.form.get('seller_email')
            listing.seller_whatsapp = request.form.get('seller_whatsapp', '')
            
            # Update coordinates if provided
            coordinates_json = request.form.get('coordinates')
            if coordinates_json:
                try:
                    geom_dict = json.loads(coordinates_json)
                    geom = shape(geom_dict)
                    listing.coordinates = from_shape(geom, srid=4326)
                except Exception as e:
                    current_app.logger.warning(f'Could not parse coordinates: {e}')
            
            # Update amenities
            amenities = []
            if request.form.get('water'):
                amenities.append('water')
            if request.form.get('electricity'):
                amenities.append('electricity')
            if request.form.get('road_access'):
                amenities.append('road_access')
            if request.form.get('fence'):
                amenities.append('fence')
            if request.form.get('title_deed'):
                amenities.append('title_deed')
            listing.amenities = amenities if amenities else None
            
            db.session.commit()
            
            log_audit('update_listing', 'available_lands', listing.id, None, {
                'title': listing.title
            })
            
            flash('Listing updated successfully!', 'success')
            return redirect(url_for('seller_dashboard'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Error updating listing')
            flash(f'Error updating listing: {str(e)}', 'danger')
    
    # GET request - show the form with existing data
    return render_template('seller/edit_land.html', listing=listing)


@app.route('/seller/listing/<int:listing_id>/delete', methods=['POST'])
@login_required
def delete_listing(listing_id):
    """Delete a land listing."""
    if current_user.role != 'seller':
        flash('Only sellers can delete listings.', 'danger')
        return redirect(url_for('index'))
    
    listing = AvailableLand.query.get_or_404(listing_id)
    
    # Security check: ensure the current user owns this listing
    if listing.seller_id != current_user.id:
        flash('You do not have permission to delete this listing.', 'danger')
        return redirect(url_for('seller_dashboard'))
    
    try:
        # Mark as deleted (soft delete) rather than actually deleting
        listing.status = 'deleted'
        db.session.commit()
        
        log_audit('delete_listing', 'available_lands', listing.id, None, {
            'title': listing.title,
            'reference': listing.listing_reference
        })
        
        flash('Listing deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Error deleting listing')
        flash(f'Error deleting listing: {str(e)}', 'danger')
    
    return redirect(url_for('seller_dashboard'))


@app.route('/available_lands')
@app.route('/available-lands')  # Add hyphenated alias for URL consistency
def available_lands():
    """Browse available lands - public page."""
    return render_template('available_lands.html')


# --- API Endpoints for Available Lands ---
@app.route('/api/available-lands')
def api_available_lands():
    """API endpoint to get available lands listings."""
    try:
        land_type = request.args.get('land_type', 'unregistered').lower()
        property_type = request.args.get('property_type', '').lower()
        land_use = request.args.get('land_use', '').lower()
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        min_size = request.args.get('min_size', type=float)
        
        # Base query - ONLY show approved and active listings to public
        query = AvailableLand.query.filter(
            AvailableLand.status == 'active',
            AvailableLand.admin_approval_status == 'approved'
        )
        
        # Filter by land type
        if land_type == 'registered':
            query = query.filter(AvailableLand.is_registered == True)
        else:  # unregistered
            query = query.filter(AvailableLand.is_registered == False)
        
        # Filter by property type
        if property_type and property_type != '':
            query = query.filter(AvailableLand.property_type == property_type)
        
        # Filter by land use
        if land_use and land_use != '':
            query = query.filter(AvailableLand.land_use == land_use)
        
        # Filter by price range
        if min_price is not None:
            query = query.filter(AvailableLand.asking_price >= min_price)
        if max_price is not None:
            query = query.filter(AvailableLand.asking_price <= max_price)
        
        # Filter by minimum size
        if min_size is not None:
            query = query.filter(AvailableLand.size >= min_size)
        
        listings = query.order_by(AvailableLand.created_at.desc()).all()
        
        # Increment view count for each listing
        for listing in listings:
            listing.view_count = (listing.view_count or 0) + 1
        db.session.commit()
        
        # Build response
        result = {
            'listings': []
        }
        
        for listing in listings:
            # Get first image if available
            listing_image = None
            if listing.images and len(listing.images) > 0:
                listing_image = f"/static/listings/{listing.listing_reference}/{listing.images[0]}"
            
            result['listings'].append({
                'id': listing.id,
                'listing_reference': listing.listing_reference,
                'title': listing.title,
                'description': listing.description[:200] + '...' if len(listing.description) > 200 else listing.description,
                'location': listing.location,
                'size': listing.size,
                'property_type': listing.property_type,
                'land_use': listing.land_use,
                'asking_price': listing.asking_price,
                'is_registered': listing.is_registered,
                'seller_name': listing.seller_name,
                'seller_phone': listing.seller_phone,
                'seller_email': listing.seller_email,
                'amenities': listing.amenities or [],
                'image': listing_image,
                'details_url': url_for('land_details', listing_id=listing.id),
                'view_count': listing.view_count
            })
        
        return jsonify(result)
    
    except Exception as e:
        current_app.logger.exception('Error fetching available lands')
        return jsonify({'error': str(e), 'listings': []}), 500


@app.route('/api/available-lands-geojson')
def api_available_lands_geojson():
    """API endpoint to get available lands as GeoJSON for map display."""
    try:
        land_type = request.args.get('land_type', 'unregistered').lower()
        
        # Base query - ONLY show approved and active listings with coordinates
        query = AvailableLand.query.filter(
            AvailableLand.status == 'active',
            AvailableLand.admin_approval_status == 'approved',
            AvailableLand.coordinates != None
        )
        
        # Filter by land type
        if land_type == 'registered':
            query = query.filter(AvailableLand.is_registered == True)
        else:  # unregistered
            query = query.filter(AvailableLand.is_registered == False)
        
        listings = query.all()
        
        # Build GeoJSON FeatureCollection
        features = []
        for listing in listings:
            try:
                if listing.coordinates:
                    geom = to_shape(listing.coordinates)
                    
                    # Get centroid for marker placement
                    centroid = geom.centroid
                    
                    feature = {
                        'type': 'Feature',
                        'geometry': {
                            'type': 'Point',
                            'coordinates': [centroid.x, centroid.y]
                        },
                        'properties': {
                            'id': listing.id,
                            'title': listing.title,
                            'location': listing.location,
                            'size': listing.size,
                            'asking_price': listing.asking_price,
                            'property_type': listing.property_type,
                            'is_registered': listing.is_registered,
                            'seller_name': listing.seller_name,
                            'seller_phone': listing.seller_phone,
                            'seller_email': listing.seller_email,
                            'land_use': listing.land_use,
                            'details_url': url_for('land_details', listing_id=listing.id)
                        }
                    }
                    features.append(feature)
            except Exception as e:
                current_app.logger.error(f'Error processing listing {listing.id}: {e}')
        
        # Return FeatureCollection
        geojson = {
            'type': 'FeatureCollection',
            'features': features
        }
        
        return jsonify(geojson)
    
    except Exception as e:
        current_app.logger.exception('Error generating GeoJSON')
        return jsonify({'type': 'FeatureCollection', 'features': []}), 500


@app.route('/api/available-lands-debug')
def api_available_lands_debug():
    """Debug endpoint to check available lands data in database."""
    try:
        total = AvailableLand.query.count()
        active = AvailableLand.query.filter_by(status='active').count()
        pending = AvailableLand.query.filter_by(status='pending').count()
        approved = AvailableLand.query.filter_by(admin_approval_status='approved').count()
        pending_approval = AvailableLand.query.filter_by(admin_approval_status='pending').count()
        registered = AvailableLand.query.filter_by(is_registered=True).count()
        unregistered = AvailableLand.query.filter_by(is_registered=False).count()
        
        # Get sample listings
        samples = AvailableLand.query.limit(5).all()
        sample_data = []
        for s in samples:
            sample_data.append({
                'id': s.id,
                'reference': s.listing_reference,
                'status': s.status,
                'admin_approval_status': s.admin_approval_status,
                'is_registered': s.is_registered,
                'title': s.title,
                'location': s.location
            })
        
        return jsonify({
            'database_stats': {
                'total_listings': total,
                'by_status': {'active': active, 'pending': pending},
                'by_approval': {'approved': approved, 'pending': pending_approval},
                'by_registration': {'registered': registered, 'unregistered': unregistered}
            },
            'sample_listings': sample_data
        })
    except Exception as e:
        current_app.logger.exception('Debug endpoint error')
        return jsonify({'error': str(e)}), 500


@app.route('/land_details/<int:listing_id>')
def land_details(listing_id):
    """View details of a specific land listing."""
    listing = AvailableLand.query.get_or_404(listing_id)
    
    # Check if listing is active and approved
    if listing.status != 'active' or listing.admin_approval_status != 'approved':
        if not current_user.is_authenticated or (current_user.id != listing.seller_id and current_user.role not in ['admin', 'super_admin']):
            flash('This listing is not available.', 'danger')
            return redirect(url_for('available_lands'))
    
    # Increment view count
    listing.view_count = (listing.view_count or 0) + 1
    db.session.commit()
    
    return render_template('land_details.html', listing=listing)


# --- Admin: Seller Listings Management ---
@app.route('/admin/seller-listings')
@login_required
def admin_seller_listings():
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    status_filter = request.args.get('status', 'pending')
    q = AvailableLand.query
    if status_filter == 'pending':
        q = q.filter(AvailableLand.admin_approval_status == 'pending')
    elif status_filter == 'approved':
        q = q.filter(AvailableLand.admin_approval_status == 'approved')
    elif status_filter == 'rejected':
        q = q.filter(AvailableLand.admin_approval_status == 'rejected')

    listings = q.order_by(AvailableLand.created_at.desc()).all()

    pending_count = AvailableLand.query.filter_by(admin_approval_status='pending').count()
    approved_count = AvailableLand.query.filter_by(admin_approval_status='approved').count()
    rejected_count = AvailableLand.query.filter_by(admin_approval_status='rejected').count()

    return render_template('admin_seller_listings.html', listings=listings,
                           status_filter=status_filter,
                           pending_count=pending_count,
                           approved_count=approved_count,
                           rejected_count=rejected_count)


@app.route('/admin/seller-listing/<int:listing_id>')
@login_required
def view_seller_listing_admin(listing_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    listing = AvailableLand.query.get_or_404(listing_id)

    # Convert coordinates to GeoJSON if present
    coordinates_geojson = None
    try:
        if listing.coordinates:
            geom = to_shape(listing.coordinates)
            coordinates_geojson = geom.__geo_interface__
    except Exception:
        coordinates_geojson = None

    return render_template('admin_view_listing.html', listing=listing, listing_id=listing.id, listing_reference=listing.listing_reference,
                           listing_coordinates_geojson=coordinates_geojson,
                           listing_coordinates=coordinates_geojson,
                           coordinates_geojson=coordinates_geojson,
                           now=datetime.utcnow)


@app.route('/admin/seller-listing/<int:listing_id>/approve', methods=['POST'])
@login_required
def approve_seller_listing(listing_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    listing = AvailableLand.query.get_or_404(listing_id)
    comments = request.form.get('admin_comments')
    try:
        listing.admin_approval_status = 'approved'
        listing.status = 'active'
        listing.approved_by = current_user.id
        listing.approved_at = datetime.utcnow()

        # Save approval note into admin_comments JSON
        ac = listing.admin_comments or {}
        ac.setdefault('approval', {})
        ac['approval'] = {
            'admin_id': current_user.id,
            'admin_name': current_user.get_full_name(),
            'comment': comments or '',
            'timestamp': datetime.utcnow().isoformat()
        }
        listing.admin_comments = ac

        db.session.add(listing)
        db.session.commit()
        log_audit('approve_seller_listing', 'available_lands', listing.id, None, {'approved_by': current_user.id})
        flash('Listing approved and is now active.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Failed to approve listing')
        flash(f'Failed to approve listing: {e}', 'danger')

    return redirect(url_for('admin_seller_listings'))


@app.route('/admin/seller-listing/<int:listing_id>/reject', methods=['POST'])
@login_required
def reject_seller_listing(listing_id):
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    listing = AvailableLand.query.get_or_404(listing_id)
    reason = request.form.get('rejection_reason') or 'Rejected by admin'
    try:
        listing.admin_approval_status = 'rejected'
        listing.status = 'rejected'

        ac = listing.admin_comments or {}
        ac.setdefault('rejection', {})
        ac['rejection'] = {
            'admin_id': current_user.id,
            'admin_name': current_user.get_full_name(),
            'reason': reason,
            'timestamp': datetime.utcnow().isoformat()
        }
        listing.admin_comments = ac

        db.session.add(listing)
        db.session.commit()
        log_audit('reject_seller_listing', 'available_lands', listing.id, None, {'rejected_by': current_user.id})
        flash('Listing rejected.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Failed to reject listing')
        flash(f'Failed to reject listing: {e}', 'danger')

    return redirect(url_for('admin_seller_listings'))


@app.route('/admin/seller-listing/<int:listing_id>/comment', methods=['POST'])
@login_required
def add_listing_comment(listing_id):
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'error': 'Access denied'}), 403

    listing = AvailableLand.query.get_or_404(listing_id)
    comment = request.form.get('comment')
    if not comment:
        flash('Comment cannot be empty.', 'danger')
        return redirect(url_for('view_seller_listing_admin', listing_id=listing_id))

    try:
        ac = listing.admin_comments or {}
        comments = ac.get('comments', [])
        comments.insert(0, {
            'admin_id': current_user.id,
            'admin_name': current_user.get_full_name(),
            'comment': comment,
            'timestamp': datetime.utcnow().isoformat()
        })
        ac['comments'] = comments
        listing.admin_comments = ac
        db.session.add(listing)
        db.session.commit()
        flash('Comment added.', 'success')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to add listing comment')
        flash('Failed to add comment.', 'danger')

    return redirect(url_for('view_seller_listing_admin', listing_id=listing_id))


@app.route('/manual_land_entry', methods=['GET', 'POST'])
@login_required
def manual_land_entry():
    if current_user.role not in ['admin', 'super_admin']:
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        try:
            # Create an application reference with LR-M- prefix
            reference = f"LR-M-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

            applicant_name = request.form.get('applicant_name') or 'Manual Entry'
            nrc = request.form.get('nrc_number') or ''
            phone = request.form.get('phone_number') or ''
            email = request.form.get('email') or ''
            tpin = request.form.get('tpin_number') or ''
            location = request.form.get('land_location') or 'Unknown'
            size = float(request.form.get('land_size', 0) or 0)
            land_use = request.form.get('land_use') or 'unspecified'
            description = request.form.get('land_description') or ''
            geom_json = request.form.get('land_geometry')

            coords_wkb = None
            if geom_json:
                try:
                    geom_dict = json.loads(geom_json)
                    geom = shape(geom_dict)
                    coords_wkb = from_shape(geom, srid=4326)
                except Exception:
                    coords_wkb = None

            # Create a LandApplication record (manual admin entry should not create a public listing)
            application = LandApplication(
                reference_number=reference,
                applicant_name=applicant_name,
                nrc_number=nrc,
                tpin_number=tpin,
                phone_number=phone,
                email=email,
                land_location=location,
                land_size=size,
                land_use=land_use,
                land_description=description,
                declared_value=0.0,
                status='approved',
                priority='medium',
                ai_conflict_score=0.0,
                ai_duplicate_score=0.0,
                ai_processed=False,
                processing_fee=0.0,
                payment_status='paid',
                admin_comments='Manual admin entry',
                submitted_at=datetime.utcnow(),
                reviewed_at=datetime.utcnow(),
                approved_at=datetime.utcnow(),
                user_id=None,
                registration_type='manual'
            )

            if coords_wkb:
                application.coordinates = coords_wkb

            db.session.add(application)
            db.session.commit()
            log_audit('manual_land_entry', 'land_applications', application.id, None, {'created_by': current_user.id})
            flash('Manual land application saved and approved (not a public listing).', 'success')
            return redirect(url_for('admin_dashboard'))
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to create manual land entry')
            flash('Failed to save manual entry.', 'danger')

    return render_template('admin_manual_entry.html')


@app.route('/admin/debug/available-lands')
@login_required
def admin_debug_available_lands():
    """Admin-only debug endpoint returning counts and recent AvailableLand rows.

    Use this to quickly inspect which rows are active/approved and whether
    geometry/flags match the API filters.
    """
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'error': 'Access denied'}), 403

    try:
        total = AvailableLand.query.count()
        approved_active = AvailableLand.query.filter(
            AvailableLand.admin_approval_status == 'approved',
            AvailableLand.status == 'active'
        ).count()
        pending = AvailableLand.query.filter(AvailableLand.admin_approval_status == 'pending').count()
        rejected = AvailableLand.query.filter(AvailableLand.admin_approval_status == 'rejected').count()

        samples = AvailableLand.query.order_by(AvailableLand.created_at.desc()).limit(50).all()
        rows = []
        for s in samples:
            rows.append({
                'id': s.id,
                'listing_reference': getattr(s, 'listing_reference', None),
                'status': getattr(s, 'status', None),
                'admin_approval_status': getattr(s, 'admin_approval_status', None),
                'is_registered': getattr(s, 'is_registered', None),
                'property_type': getattr(s, 'property_type', None),
                'land_use': getattr(s, 'land_use', None),
                'asking_price': getattr(s, 'asking_price', None),
                'size': getattr(s, 'size', None),
                'has_coordinates': bool(getattr(s, 'coordinates', None)),
                'latitude': getattr(s, 'latitude', None),
                'longitude': getattr(s, 'longitude', None),
                'seller_id': getattr(s, 'seller_id', None),
                'created_at': getattr(s, 'created_at').isoformat() if getattr(s, 'created_at', None) else None
            })

        return jsonify({
            'total': total,
            'approved_active': approved_active,
            'pending': pending,
            'rejected': rejected,
            'samples': rows
        })
    except Exception:
        current_app.logger.exception('Debug endpoint failed')
        return jsonify({'error': 'internal'}), 500


# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(error):
    """Handles 404 errors."""
    return render_template('404.html'), 404


@app.errorhandler(500)
def internal_server_error(error):
    """Handles 500 errors."""
    db.session.rollback()
    return render_template('500.html'), 500


@app.errorhandler(413)
def file_too_large(error):
    """Handles file upload size errors."""
    flash('File too large. Maximum size is 16MB.', 'danger')
    return redirect(url_for('register_land'))


# --- Context Processors ---
@app.context_processor
def inject_global_vars():
    """Injects global variables into templates."""
    return {
        'system_name': SystemSettings.get_setting('system_name', 'Ndola Land Registry System'),
        'current_year': datetime.utcnow().year
    }


if __name__ == '__main__':
    # Start background notification worker when running as main
    try:
        start_notification_worker()
    except Exception:
        app.logger.exception('Could not start notification worker')
    app.run(debug=True)