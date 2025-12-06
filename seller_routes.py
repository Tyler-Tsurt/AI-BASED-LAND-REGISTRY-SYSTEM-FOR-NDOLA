# Seller Routes for Available Lands Management
from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from functools import wraps
from models import db, AvailableLand, User
from shapely.geometry import shape, Point, mapping
from geoalchemy2.shape import from_shape, to_shape
from datetime import datetime
import json

def seller_required(f):
    """Decorator to require seller role ONLY - admins cannot post lands."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        # STRICT: Only sellers can access these routes, NOT admins
        if current_user.role != 'seller':
            flash('Only sellers can post and manage land listings. Admins should use the admin interface at /admin/seller-listings', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def register_seller_routes(app):
    """Register all seller-related routes."""
    
    @app.route('/seller/dashboard')
    @login_required
    @seller_required
    def seller_dashboard():
        """Seller dashboard showing ONLY their own listings."""
        # IMPORTANT: Only show listings created by the current seller
        listings = AvailableLand.query.filter_by(
            seller_id=current_user.id
        ).order_by(AvailableLand.created_at.desc()).all()
        
        # Calculate statistics
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
    
    
    @app.route('/seller/post-land', methods=['GET', 'POST'])
    @login_required
    @seller_required
    def post_land():
        """Form to post a new available land listing."""
        if request.method == 'POST':
            try:
                # Extract form data
                title = request.form.get('title')
                description = request.form.get('description')
                location = request.form.get('location')
                size = float(request.form.get('size'))
                land_use = request.form.get('land_use')
                asking_price = float(request.form.get('asking_price'))
                property_type = request.form.get('property_type')
                is_registered = request.form.get('is_registered') == 'yes'
                
                # Seller contact info (use current user's info)
                seller_phone = request.form.get('seller_phone') or current_user.phone_number
                seller_email = request.form.get('seller_email') or current_user.email
                seller_whatsapp = request.form.get('seller_whatsapp')
                
                # Amenities (checkboxes)
                amenities = []
                if request.form.get('water'): amenities.append('water')
                if request.form.get('electricity'): amenities.append('electricity')
                if request.form.get('road_access'): amenities.append('road_access')
                if request.form.get('fence'): amenities.append('fence')
                if request.form.get('title_deed'): amenities.append('title_deed')
                
                # Coordinates (optional)
                coordinates_geojson = request.form.get('coordinates')
                coordinates = None
                latitude = None
                longitude = None
                
                if coordinates_geojson:
                    try:
                        coords_data = json.loads(coordinates_geojson)
                        geom = shape(coords_data)
                        coordinates = from_shape(geom, srid=4326)
                        
                        # Get centroid for lat/lng
                        centroid = geom.centroid
                        latitude = centroid.y
                        longitude = centroid.x
                    except Exception as e:
                        flash(f'Invalid coordinates format: {str(e)}', 'warning')
                
                # Create new listing
                new_listing = AvailableLand(
                    title=title,
                    description=description,
                    location=location,
                    size=size,
                    land_use=land_use,
                    asking_price=asking_price,
                    property_type=property_type,
                    is_registered=is_registered,
                    seller_id=current_user.id,
                    seller_name=current_user.get_full_name(),
                    seller_phone=seller_phone,
                    seller_email=seller_email,
                    seller_whatsapp=seller_whatsapp,
                    amenities=amenities,
                    coordinates=coordinates,
                    latitude=latitude,
                    longitude=longitude,
                    status='pending',
                    admin_approval_status='pending'
                )
                
                # Generate listing reference
                new_listing.listing_reference = new_listing.generate_listing_reference()
                
                db.session.add(new_listing)
                db.session.commit()
                
                flash(f'Land listing posted successfully! Reference: {new_listing.listing_reference}. Your listing is pending admin approval.', 'success')
                return redirect(url_for('seller_dashboard'))
                
            except Exception as e:
                db.session.rollback()
                flash(f'Error posting listing: {str(e)}', 'danger')
        
        return render_template('seller/post_land.html')
    
    
    @app.route('/seller/edit-listing/<int:listing_id>', methods=['GET', 'POST'])
    @login_required
    @seller_required
    def edit_listing(listing_id):
        """Edit an existing land listing - ONLY if you own it."""
        listing = AvailableLand.query.get_or_404(listing_id)
        
        # STRICT ownership check - sellers can ONLY edit their own listings
        if listing.seller_id != current_user.id:
            flash('You can only edit your own listings.', 'danger')
            return redirect(url_for('seller_dashboard'))
        
        if request.method == 'POST':
            try:
                # Update listing
                listing.title = request.form.get('title')
                listing.description = request.form.get('description')
                listing.location = request.form.get('location')
                listing.size = float(request.form.get('size'))
                listing.land_use = request.form.get('land_use')
                listing.asking_price = float(request.form.get('asking_price'))
                listing.property_type = request.form.get('property_type')
                listing.is_registered = request.form.get('is_registered') == 'yes'
                listing.seller_phone = request.form.get('seller_phone')
                listing.seller_email = request.form.get('seller_email')
                listing.seller_whatsapp = request.form.get('seller_whatsapp')
                
                # Update amenities
                amenities = []
                if request.form.get('water'): amenities.append('water')
                if request.form.get('electricity'): amenities.append('electricity')
                if request.form.get('road_access'): amenities.append('road_access')
                if request.form.get('fence'): amenities.append('fence')
                if request.form.get('title_deed'): amenities.append('title_deed')
                listing.amenities = amenities
                
                listing.updated_at = datetime.utcnow()
                
                db.session.commit()
                flash('Listing updated successfully!', 'success')
                return redirect(url_for('seller_dashboard'))
                
            except Exception as e:
                db.session.rollback()
                flash(f'Error updating listing: {str(e)}', 'danger')
        
        return render_template('seller/edit_listing.html', listing=listing)
    
    
    @app.route('/seller/delete-listing/<int:listing_id>', methods=['POST'])
    @login_required
    @seller_required
    def delete_listing(listing_id):
        """Delete a land listing - ONLY if you own it."""
        listing = AvailableLand.query.get_or_404(listing_id)
        
        # STRICT ownership check - sellers can ONLY delete their own listings
        if listing.seller_id != current_user.id:
            flash('You can only delete your own listings.', 'danger')
            return redirect(url_for('seller_dashboard'))
        
        try:
            db.session.delete(listing)
            db.session.commit()
            flash('Listing deleted successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error deleting listing: {str(e)}', 'danger')
        
        return redirect(url_for('seller_dashboard'))
    
    
    @app.route('/available-lands')
    def available_lands():
        """Public page showing all available lands."""
        return render_template('available_lands.html')
    
    
    @app.route('/land-details/<int:listing_id>')
    def land_details(listing_id):
        """Show detailed information about a specific land listing."""
        listing = AvailableLand.query.get_or_404(listing_id)
        
        # Increment view count
        listing.view_count += 1
        db.session.commit()
        
        # Convert coordinates to GeoJSON for display
        coordinates_geojson = None
        if listing.coordinates:
            try:
                geom = to_shape(listing.coordinates)
                coordinates_geojson = mapping(geom)
            except Exception as e:
                print(f"Error converting coordinates: {e}")
        
        # Attach GeoJSON to listing object for template
        listing.coordinates_geojson = coordinates_geojson
        
        # Get similar listings
        similar_listings = AvailableLand.query.filter(
            AvailableLand.id != listing_id,
            AvailableLand.status == 'active',
            AvailableLand.admin_approval_status == 'approved',
            AvailableLand.property_type == listing.property_type
        ).limit(3).all()
        
        return render_template('land_details.html', 
                             listing=listing,
                             similar_listings=similar_listings)
    
    
    @app.route('/api/available-lands')
    def api_available_lands():
        """API endpoint to get available lands filtered by type (registered/unregistered)."""
        land_type = request.args.get('land_type', 'unregistered')  # Default to unregistered
        
        # Build base query - ONLY approved and active listings
        query = AvailableLand.query.filter_by(
            status='active', 
            admin_approval_status='approved'
        )
        
        # Filter by registration status
        if land_type == 'registered':
            query = query.filter_by(is_registered=True)
        else:  # unregistered
            query = query.filter_by(is_registered=False)
        
        listings = query.order_by(
            AvailableLand.featured.desc(),
            AvailableLand.created_at.desc()
        ).all()
        
        # Convert to JSON-serializable format
        results = []
        for listing in listings:
            results.append({
                'id': listing.id,
                'listing_reference': listing.listing_reference,
                'title': listing.title,
                'description': listing.description,
                'location': listing.location,
                'size': listing.size,
                'asking_price': listing.asking_price,
                'property_type': listing.property_type,
                'land_use': listing.land_use,
                'is_registered': listing.is_registered,
                'seller_name': listing.seller_name,
                'seller_phone': listing.seller_phone,
                'amenities': listing.amenities or [],
                'details_url': url_for('land_details', listing_id=listing.id)
            })
        
        return jsonify({
            'success': True,
            'land_type': land_type,
            'count': len(results),
            'listings': results
        })
    
    
    @app.route('/api/available-lands-geojson')
    def api_available_lands_geojson():
        """API endpoint to get all available lands as GeoJSON for map display."""
        land_type = request.args.get('land_type', 'unregistered')  # Default to unregistered
        
        # Build query with land type filter - ONLY approved and active
        query = AvailableLand.query.filter_by(
            status='active', 
            admin_approval_status='approved'
        )
        
        # Filter by registration status
        if land_type == 'registered':
            query = query.filter_by(is_registered=True)
        else:  # unregistered
            query = query.filter_by(is_registered=False)
        
        listings = query.all()
        
        features = []
        for listing in listings:
            if listing.latitude and listing.longitude:
                # Use point for marker
                feature = {
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [listing.longitude, listing.latitude]
                    },
                    'properties': {
                        'id': listing.id,
                        'listing_reference': listing.listing_reference,
                        'title': listing.title,
                        'location': listing.location,
                        'size': listing.size,
                        'asking_price': listing.asking_price,
                        'property_type': listing.property_type,
                        'land_use': listing.land_use,
                        'seller_name': listing.seller_name,
                        'seller_phone': listing.seller_phone,
                        'is_registered': listing.is_registered,
                        'details_url': url_for('land_details', listing_id=listing.id)
                    }
                }
                features.append(feature)
        
        geojson = {
            'type': 'FeatureCollection',
            'features': features
        }
        
        return jsonify(geojson)
