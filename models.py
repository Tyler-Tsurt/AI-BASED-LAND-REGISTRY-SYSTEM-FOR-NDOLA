from flask_sqlalchemy import SQLAlchemy
from geoalchemy2 import Geometry
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy import Enum
import secrets

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    # FIX: Increased the size of password_hash to accommodate longer hashes like scrypt
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='citizen')  # citizen, admin, super_admin, seller
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    phone_number = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Relationships - Fixed foreign key specifications
    applications = db.relationship('LandApplication',
                                  foreign_keys='LandApplication.user_id',
                                  backref='applicant_user',
                                  lazy=True)
    reviewed_applications = db.relationship('LandApplication',
                                          foreign_keys='LandApplication.reviewed_by',
                                          backref='reviewer',
                                          lazy=True)

    def set_password(self, password):
        """Hashes and sets the user's password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Checks the provided password against the stored hash."""
        return check_password_hash(self.password_hash, password)

    def get_full_name(self,):
        """Returns the user's full name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username

    def __repr__(self):
        return f'<User {self.username}>'

class LandApplication(db.Model):
    __tablename__ = 'land_applications'

    id = db.Column(db.Integer, primary_key=True)
    reference_number = db.Column(db.String(20), unique=True, nullable=False)

    applicant_name = db.Column(db.String(150), nullable=False)
    nrc_number = db.Column(db.String(50), nullable=False)
    tpin_number = db.Column(db.String(50), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    land_location = db.Column(db.Text, nullable=False)
    land_size = db.Column(db.Float, nullable=False)
    land_use = db.Column(db.String(50), nullable=False)
    land_description = db.Column(db.Text)
    registration_type = db.Column(db.String(50), nullable=False)

    # Financial fields
    declared_value = db.Column(db.Float, default=0.0)
    secured_amount = db.Column(db.Float, nullable=True)
    annual_rent = db.Column(db.Float, nullable=True)


    # Geospatial data
    coordinates = db.Column(Geometry('POLYGON', srid=4326))

    # Application Status
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected, under_review
    priority = db.Column(db.String(10), default='medium')  # low, medium, high

    # AI Analysis
    ai_conflict_score = db.Column(db.Float, default=0.0)
    ai_duplicate_score = db.Column(db.Float, default=0.0)
    ai_analysis_result = db.Column(db.JSON)
    ai_processed = db.Column(db.Boolean, default=False)

    # Processing Information
    processing_fee = db.Column(db.Float, default=500.0)  # in ZMW
    payment_status = db.Column(db.String(20), default='pending')  # pending, paid, failed

    # Comments and Notes
    admin_comments = db.Column(db.Text)
    rejection_reason = db.Column(db.Text)

    # Timestamps
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    approved_at = db.Column(db.DateTime)

    # Foreign Keys
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Relationships
    documents = db.relationship('Document', backref='application', lazy=True, cascade='all, delete-orphan')

    land_parcel = db.relationship('LandParcel', 
                              backref='land_application',
                              foreign_keys='LandParcel.application_id',
                              uselist=False)
    
    # FIX: Explicitly specify the foreign key for this relationship to avoid the AmbiguousForeignKeysError.
    conflicts = db.relationship('LandConflict', foreign_keys='LandConflict.application_id', backref='application', lazy=True)



    def generate_reference_number(self):
        """Generate a unique reference number for the application."""
        year = datetime.utcnow().year
        count = LandApplication.query.filter(
            LandApplication.reference_number.like(f'LR-{year}-%')
        ).count() + 1
        return f'LR-{year}-{count:04d}'

    def get_status_badge_class(self):
        """Return Bootstrap badge class based on status."""
        status_classes = {
            'pending': 'bg-warning',
            'under_review': 'bg-info',
            'approved': 'bg-success',
            'rejected': 'bg-danger'
        }
        return status_classes.get(self.status, 'bg-secondary')

    def get_priority_badge_class(self):
        """Return Bootstrap badge class based on priority."""
        priority_classes = {
            'low': 'bg-success',
            'medium': 'bg-warning',
            'high': 'bg-danger'
        }
        return priority_classes.get(self.priority, 'bg-secondary')

    def __repr__(self):
        return f'<LandApplication {self.reference_number}>'

class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('land_applications.id'), nullable=False)
    document_type = db.Column(db.String(100), nullable=False)  # <-- changed from ENUM to String
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer)
    mime_type = db.Column(db.String(100))
    file_hash = db.Column(db.String(64))  # SHA-256 hash
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class LandParcel(db.Model):
    __tablename__ = 'land_parcels'

    id = db.Column(db.Integer, primary_key=True)
    parcel_number = db.Column(db.String(50), unique=True, nullable=False)
    owner_name = db.Column(db.String(100))
    owner_nrc = db.Column(db.String(20))  # NRC number
    owner_phone = db.Column(db.String(20))
    owner_email = db.Column(db.String(120))
    size = db.Column(db.Float)  # in hectares
    location = db.Column(db.Text)
    certificate_number = db.Column(db.String(50), unique=True)
    land_use = db.Column(db.String(50))
    land_description = db.Column(db.Text)
    ward = db.Column(db.String(50))
    constituency = db.Column(db.String(50))
    district = db.Column(db.String(50))
    status = db.Column(db.String(20), default='registered')
    title_deed_issued = db.Column(db.Boolean, default=False)
    survey_completed = db.Column(db.Boolean, default=False)
    valuation = db.Column(db.Float)  # Valuation amount in ZMW
    annual_tax = db.Column(db.Float)  # Annual tax in ZMW
    coordinates = db.Column(Geometry('POLYGON', srid=4326))
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Foreign Keys
    application_id = db.Column(db.Integer, db.ForeignKey('land_applications.id'))

    # Relationships
    conflicts = db.relationship(
        'LandConflict',
        foreign_keys='LandConflict.conflicting_parcel_id',
        backref='conflicting_parcel',
        lazy=True
    )

    def __repr__(self):
        return f'<LandParcel {self.parcel_number}>'

class LandConflict(db.Model):
    __tablename__ = 'land_conflicts'

    id = db.Column(db.Integer, primary_key=True)
    # This foreign key links to the application that is being checked for conflicts.
    application_id = db.Column(db.Integer, db.ForeignKey('land_applications.id'))
    # This foreign key links to the existing land parcel that is in conflict.
    conflicting_parcel_id = db.Column(db.Integer, db.ForeignKey('land_parcels.id'))
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='unresolved') # unresolved, resolved
    detected_by_ai = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    # FIX: Added new columns to match the sample data being inserted in init_db.py
    conflict_type = db.Column(db.String(50))
    title = db.Column(db.String(200))
    severity = db.Column(db.String(20))
    overlap_percentage = db.Column(db.Float)
    confidence_score = db.Column(db.Float)
    
    def __repr__(self):
        return f'<LandConflict {self.id}>'

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    table_name = db.Column(db.String(50))
    record_id = db.Column(db.Integer)
    old_values = db.Column(db.JSON)
    new_values = db.Column(db.JSON)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.String(500))

    user = db.relationship('User', backref='audit_logs', lazy=True)

    def __repr__(self):
        return f'<AuditLog {self.action} by user {self.user_id}>'

class SystemSettings(db.Model):
    __tablename__ = 'system_settings'

    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(100), unique=True, nullable=False)
    setting_value = db.Column(db.Text, nullable=False)
    setting_type = db.Column(db.String(20)) # string, integer, float, boolean
    description = db.Column(db.Text)
    category = db.Column(db.String(50))
    is_system = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))

    updater = db.relationship('User', backref='updated_settings', lazy=True)

    @classmethod
    def get_setting(cls, key, default=None):
        """Get setting value by key."""
        setting = cls.query.filter_by(setting_key=key).first()
        if setting:
            setting_type = setting.setting_type
            setting_value = setting.setting_value
            if setting_type == 'integer':
                return int(setting_value)
            if setting_type == 'float':
                return float(setting_value)
            if setting_type == 'boolean':
                return setting_value.lower() == 'true'
            return setting_value
        return default

    @classmethod
    def set_setting(cls, key, value, user_id=None):
        """Set setting value by key."""
        setting = cls.query.filter_by(setting_key=key).first()
        if setting:
            setting.setting_value = str(value)
            if user_id:
                setting.updated_by = user_id
        else:
            setting = cls(setting_key=key, setting_value=str(value), updated_by=user_id)
            db.session.add(setting)
        db.session.commit()

    def __repr__(self):
        return f'<SystemSettings {self.setting_key}>'

class NotificationLog(db.Model):
    __tablename__ = 'notification_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    application_id = db.Column(db.Integer, db.ForeignKey('land_applications.id'))
    notification_type = db.Column(db.String(50), nullable=False)  # email, sms, system
    recipient = db.Column(db.String(200), nullable=False)  # email address or phone number
    subject = db.Column(db.String(200))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending') # pending, sent, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at = db.Column(db.DateTime)
    
    user = db.relationship('User', backref='notification_logs', lazy=True)
    application = db.relationship('LandApplication', backref='notification_logs', lazy=True)
    
    def __repr__(self):
        return f'<NotificationLog {self.notification_type} to {self.recipient}>'

class AvailableLand(db.Model):
    __tablename__ = 'available_lands'
    
    id = db.Column(db.Integer, primary_key=True)
    listing_reference = db.Column(db.String(20), unique=True, nullable=False)
    
    # Land Information
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.Text, nullable=False)
    size = db.Column(db.Float, nullable=False)  # in hectares
    land_use = db.Column(db.String(50))  # residential, commercial, agricultural, etc.
    asking_price = db.Column(db.Float, nullable=False)  # in ZMW
    
    # Property Type
    property_type = db.Column(db.String(50), nullable=False)  # unregistered_land, plot, house, commercial_property
    is_registered = db.Column(db.Boolean, default=False)
    
    # Geospatial data
    coordinates = db.Column(Geometry('POLYGON', srid=4326), nullable=True)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    
    # Seller Information
    seller_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    seller_name = db.Column(db.String(150), nullable=False)
    seller_phone = db.Column(db.String(20), nullable=False)
    seller_email = db.Column(db.String(120), nullable=False)
    seller_whatsapp = db.Column(db.String(20))
    
    # Additional Details
    amenities = db.Column(db.JSON)  # water, electricity, road access, etc.
    images = db.Column(db.JSON)  # list of image URLs
    documents = db.Column(db.JSON)  # list of document URLs
    admin_comments = db.Column(db.JSON)  # admin comments, approval/rejection notes
    
    # Status
    status = db.Column(db.String(20), default='pending')  # pending, active, sold, withdrawn, rejected
    admin_approval_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    approved_at = db.Column(db.DateTime)
    featured = db.Column(db.Boolean, default=False)
    verified = db.Column(db.Boolean, default=False)
    
    # View count for analytics
    view_count = db.Column(db.Integer, default=0)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    sold_at = db.Column(db.DateTime)
    
    # Relationships
    seller = db.relationship('User', foreign_keys=[seller_id], backref='available_lands', lazy=True)
    approver = db.relationship('User', foreign_keys=[approved_by], backref='approved_listings', lazy=True)
    
    def generate_listing_reference(self):
        """Generate a unique listing reference number."""
        year = datetime.utcnow().year
        count = AvailableLand.query.filter(
            AvailableLand.listing_reference.like(f'AL-{year}-%')
        ).count() + 1
        return f'AL-{year}-{count:04d}'
    
    def get_status_badge_class(self):
        """Return Bootstrap badge class based on status."""
        status_classes = {
            'active': 'bg-success',
            'sold': 'bg-secondary',
            'withdrawn': 'bg-warning'
        }
        return status_classes.get(self.status, 'bg-secondary')
    
    def __repr__(self):
        return f'<AvailableLand {self.listing_reference}>'
