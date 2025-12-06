from dotenv import load_dotenv
load_dotenv()
import argparse
import sys
from flask import Flask
from models import db, User, SystemSettings
from werkzeug.security import generate_password_hash
from datetime import datetime
import os

def create_app():
    """Create Flask application for database initialization"""
    app = Flask(__name__)

    # Database configuration
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ["DATABASE_URL"]
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = os.environ["SECRET_KEY"]

    db.init_app(app)
    return app

def init_database():
    """Initialize database with tables and default data"""
    app = create_app()
    
    with app.app_context():
        try:
            # Drop all tables (use with caution in production!)
            print("Dropping existing tables...")
            db.drop_all()
            
            # Create all tables
            print("Creating database tables...")
            db.create_all()
            
            print("Creating default users...")
            
            # Create super admin user
            admin = User(
                username='admin',
                email='admin@ndolalands.gov.zm',
                first_name='System',
                last_name='Administrator',
                role='super_admin',
                phone_number='+260971000000'
            )
            admin.set_password('admin123')
            
            # Create ministry admin user
            ministry_admin = User(
                username='ministry_admin',
                email='ministry@lands.gov.zm',
                first_name='Ministry',
                last_name='Officer',
                role='admin',
                phone_number='+260971000001'
            )
            ministry_admin.set_password('ministry123')
            
            # Create city council admin
            council_admin = User(
                username='council_admin',
                email='council@ndola.gov.zm',
                first_name='Council',
                last_name='Officer',
                role='admin',
                phone_number='+260971000002'
            )
            council_admin.set_password('council123')
            
            # Create test citizen user
            citizen = User(
                username='citizen_test',
                email='citizen@example.com',
                first_name='Test',
                last_name='Citizen',
                role='citizen',
                phone_number='+260971234567'
            )
            citizen.set_password('citizen123')
            
            # Create seller user
            seller = User(
                username='seller1',
                email='seller1@example.com',
                first_name='John',
                last_name='Seller',
                role='seller',
                phone_number='+260971234568'
            )
            seller.set_password('seller123')
            
            db.session.add(admin)
            db.session.add(ministry_admin)
            db.session.add(council_admin)
            db.session.add(citizen)
            db.session.add(seller)
            
            # Create default system settings
            print("Creating system settings...")
            default_settings = [
                # AI Settings
                ('ai_conflict_threshold', '0.7', 'float', 'AI confidence threshold for conflict detection', 'ai'),
                ('ai_duplicate_threshold', '0.8', 'float', 'AI confidence threshold for duplicate detection', 'ai'),
                ('auto_approval_threshold', '0.95', 'float', 'AI confidence threshold for automatic approval', 'ai'),
                ('ai_processing_enabled', 'true', 'boolean', 'Enable AI processing for applications', 'ai'),
                
                # File Upload Settings
                ('max_file_size', '5242880', 'integer', 'Maximum file upload size in bytes (5MB)', 'upload'),
                ('allowed_file_types', 'pdf,jpg,jpeg,png', 'string', 'Allowed file types for document upload', 'upload'),
                ('upload_path', 'uploads', 'string', 'Directory for uploaded files', 'upload'),
                
                # Processing Settings
                ('processing_fee', '500.00', 'float', 'Land registration processing fee in ZMW', 'processing'),
                ('processing_time_days', '30', 'integer', 'Standard processing time in days', 'processing'),
                ('priority_processing_fee', '1000.00', 'float', 'Priority processing fee in ZMW', 'processing'),
                
                # System Settings
                ('system_name', 'Ndola Land Registry System', 'string', 'System name', 'system'),
                ('system_version', '1.0.0', 'string', 'System version', 'system'),
                ('maintenance_mode', 'false', 'boolean', 'Enable maintenance mode', 'system'),
                ('timezone', 'Africa/Lusaka', 'string', 'System timezone', 'system'),
                
                # Notification Settings
                ('email_enabled', 'true', 'boolean', 'Enable email notifications', 'notification'),
                ('sms_enabled', 'false', 'boolean', 'Enable SMS notifications', 'notification'),
                ('admin_notification_email', 'admin@ndolalands.gov.zm', 'string', 'Admin notification email', 'notification'),
                
                # Security Settings
                ('session_timeout', '3600', 'integer', 'Session timeout in seconds', 'security'),
                ('max_login_attempts', '5', 'integer', 'Maximum login attempts before lockout', 'security'),
                ('lockout_duration', '900', 'integer', 'Account lockout duration in seconds', 'security'),
                
                # GIS Settings
                ('default_srid', '4326', 'integer', 'Default spatial reference system ID', 'gis'),
                ('map_center_lat', '-12.9640', 'float', 'Default map center latitude (Ndola)', 'gis'),
                ('map_center_lng', '28.6367', 'float', 'Default map center longitude (Ndola)', 'gis'),
                ('default_zoom_level', '12', 'integer', 'Default map zoom level', 'gis'),
            ]
            
            for key, value, setting_type, desc, category in default_settings:
                setting = SystemSettings(
                    setting_key=key,
                    setting_value=value,
                    setting_type=setting_type,
                    description=desc,
                    category=category,
                    is_system=True,
                    updated_by=1  # Admin user
                )
                db.session.add(setting)
            
            # Commit all changes
            db.session.commit()
            print("✅ Database initialized successfully!")
            
            # Display created users
            print("\n=== Created Users ===")
            users = User.query.all()
            for user in users:
                print(f"  • {user.username:20} | {user.email:30} | Role: {user.role}")
            
            print(f"\n=== System Settings Created: {len(default_settings)} ===")
            
        except Exception as e:
            print(f"❌ Error during database initialization: {str(e)}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Initialize the database with tables and default users')
    parser.add_argument('--yes', '-y', action='store_true', help='Run non-interactively')
    args = parser.parse_args()

    print("=== Ndola Land Registry System - Database Initialization ===")
    print("This will DROP ALL TABLES and recreate them with default data.")
    print("⚠️  WARNING: All existing data will be lost!\n")

    proceed = False
    if args.yes:
        proceed = True
    else:
        choice = input("Do you want to proceed? (y/n): ").lower().strip()
        proceed = choice in ['y', 'yes']

    if not proceed:
        print("❌ Database initialization cancelled.")
        sys.exit(0)

    # Run init
    init_database()

    print("\n=== ✅ Database setup completed! ===")
    print("\n📝 Next steps:")
    print("   1. Run: python generate_ndola_data.py --yes")
    print("   2. Run: python generate_available_lands.py --yes")
    print("   3. Start app: flask run")
    print("\n🔐 Default login credentials:")
    print("   • Super Admin:    admin / admin123")
    print("   • Ministry Admin: ministry_admin / ministry123")
    print("   • Council Admin:  council_admin / council123")
    print("   • Test Citizen:   citizen_test / citizen123")
    print("   • Seller:         seller1 / seller123")
    print("\n⚠️  REMEMBER TO CHANGE DEFAULT PASSWORDS IN PRODUCTION!")
