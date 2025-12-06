"""
Debug Script - Check Available Lands Issue
This will test the API and show what's wrong
"""

from app import app, db
from models import AvailableLand
import json

def check_database():
    """Check what's in the database"""
    print("\n" + "="*70)
    print("CHECKING DATABASE")
    print("="*70)
    
    with app.app_context():
        total = AvailableLand.query.count()
        print(f"\nTotal listings in database: {total}")
        
        if total == 0:
            print("\n❌ DATABASE IS EMPTY!")
            print("   Run: python auto_fix_gis_maps.py")
            return False
        
        # Check by status
        active = AvailableLand.query.filter_by(status='active').count()
        pending = AvailableLand.query.filter_by(status='pending').count()
        
        print(f"  Active: {active}")
        print(f"  Pending: {pending}")
        
        # Check by approval
        approved = AvailableLand.query.filter_by(admin_approval_status='approved').count()
        pending_approval = AvailableLand.query.filter_by(admin_approval_status='pending').count()
        
        print(f"  Approved: {approved}")
        print(f"  Pending Approval: {pending_approval}")
        
        # Check what the API would return
        api_query = AvailableLand.query.filter(
            AvailableLand.status == 'active',
            AvailableLand.admin_approval_status == 'approved'
        )
        
        unregistered = api_query.filter_by(is_registered=False).count()
        registered = api_query.filter_by(is_registered=True).count()
        
        print(f"\nWhat API would return:")
        print(f"  Unregistered lands: {unregistered}")
        print(f"  Registered lands: {registered}")
        
        if active == 0 or approved == 0:
            print("\n❌ PROBLEM: No active AND approved listings!")
            print("   Solution: Run this SQL:")
            print("   UPDATE available_lands SET status='active', admin_approval_status='approved';")
            return False
        
        # Show sample listings
        print("\nSample listings:")
        listings = AvailableLand.query.limit(3).all()
        for listing in listings:
            print(f"\n  {listing.listing_reference}: {listing.title}")
            print(f"    Status: {listing.status}")
            print(f"    Approval: {listing.admin_approval_status}")
            print(f"    Registered: {listing.is_registered}")
            print(f"    Has coords: {bool(listing.coordinates)}")
        
        return True

def test_api_endpoints():
    """Test the API endpoints"""
    print("\n" + "="*70)
    print("TESTING API ENDPOINTS")
    print("="*70)
    
    with app.test_client() as client:
        # Test 1: Unregistered lands
        print("\nTest 1: GET /api/available-lands?land_type=unregistered")
        response = client.get('/api/available-lands?land_type=unregistered')
        print(f"  Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.get_json()
            count = len(data.get('listings', []))
            print(f"  ✅ SUCCESS - Returned {count} listings")
            if count > 0:
                print(f"  Sample: {data['listings'][0]['title']}")
        else:
            print(f"  ❌ FAILED - Status {response.status_code}")
            print(f"  Response: {response.data[:200]}")
        
        # Test 2: Registered lands
        print("\nTest 2: GET /api/available-lands?land_type=registered")
        response = client.get('/api/available-lands?land_type=registered')
        print(f"  Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.get_json()
            count = len(data.get('listings', []))
            print(f"  ✅ SUCCESS - Returned {count} listings")
        else:
            print(f"  ❌ FAILED")
        
        # Test 3: GeoJSON
        print("\nTest 3: GET /api/available-lands-geojson?land_type=unregistered")
        response = client.get('/api/available-lands-geojson?land_type=unregistered')
        print(f"  Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.get_json()
            count = len(data.get('features', []))
            print(f"  ✅ SUCCESS - Returned {count} features")
        else:
            print(f"  ❌ FAILED")

def check_routes():
    """Check if routes exist"""
    print("\n" + "="*70)
    print("CHECKING FLASK ROUTES")
    print("="*70)
    
    with app.app_context():
        routes = []
        for rule in app.url_map.iter_rules():
            if 'available' in rule.rule:
                routes.append(str(rule))
        
        print("\nAvailable lands routes found:")
        for route in routes:
            print(f"  {route}")
        
        if not routes:
            print("  ❌ NO ROUTES FOUND!")

def show_solution():
    """Show the solution"""
    print("\n" + "="*70)
    print("SOLUTION")
    print("="*70)
    
    with app.app_context():
        total = AvailableLand.query.count()
        active_approved = AvailableLand.query.filter(
            AvailableLand.status == 'active',
            AvailableLand.admin_approval_status == 'approved'
        ).count()
        
        if total == 0:
            print("\n1. DATABASE IS EMPTY")
            print("   Run: python auto_fix_gis_maps.py")
        elif active_approved == 0:
            print("\n1. LISTINGS EXIST BUT NOT ACTIVE/APPROVED")
            print("   Option A: Run SQL:")
            print("   UPDATE available_lands SET status='active', admin_approval_status='approved';")
            print("\n   Option B: Login as admin and approve listings manually at:")
            print("   http://localhost:5000/admin/seller-listings")
        else:
            print("\n✅ DATABASE LOOKS GOOD!")
            print(f"   {active_approved} listings are active and approved")
            print("\nIf citizens still can't see listings:")
            print("1. Check browser console (F12) for JavaScript errors")
            print("2. Make sure you're accessing: http://localhost:5000/available_lands")
            print("3. Try refreshing page with Ctrl+Shift+R (hard refresh)")

def main():
    """Run all checks"""
    print("\n" + "#"*70)
    print("#" + " "*68 + "#")
    print("#" + "  AVAILABLE LANDS - DIAGNOSTIC TOOL".center(68) + "#")
    print("#" + " "*68 + "#")
    print("#"*70)
    
    db_ok = check_database()
    check_routes()
    
    if db_ok:
        test_api_endpoints()
    
    show_solution()
    
    print("\n" + "="*70)
    print("DIAGNOSTIC COMPLETE")
    print("="*70 + "\n")

if __name__ == '__main__':
    main()
