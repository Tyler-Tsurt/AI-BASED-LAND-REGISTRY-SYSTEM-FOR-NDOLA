"""
duplicate_detector.py

Detects duplicate applications based on identity and document analysis.
"""
import logging
from datetime import datetime
from typing import List

from sqlalchemy import or_

from models import db, LandApplication, Document, LandConflict, AuditLog

logger = logging.getLogger(__name__)


def check_identity_duplicate(nrc_number: str, tpin_number: str = None) -> List[LandApplication]:
    """
    Check if an NRC/TPIN already exists in other applications.
    
    Args:
        nrc_number: National Registration Card number
        tpin_number: Tax Payer Identification Number (optional)
    
    Returns:
        List of applications with matching NRC or TPIN
    """
    if not nrc_number:
        return []
    
    try:
        # Build query for matching NRC or TPIN
        filters = [LandApplication.nrc_number == nrc_number]
        
        if tpin_number:
            filters.append(LandApplication.tpin_number == tpin_number)
        
        # Find matching applications
        duplicates = LandApplication.query.filter(
            or_(*filters),
            LandApplication.status != 'rejected'  # Don't include rejected apps
        ).all()
        
        logger.info(f"Found {len(duplicates)} applications with matching NRC/TPIN")
        return duplicates
    
    except Exception as e:
        logger.exception(f"Error checking identity duplicates: {e}")
        return []


def detect_all_duplicates(application_id: int) -> List[LandConflict]:
    """
    Detect all types of duplicates for an application.
    
    Checks:
    1. Identity duplicates (same NRC/TPIN)
    2. Document hash duplicates (same file uploaded multiple times)
    3. Location duplicates (same location description)
    
    Args:
        application_id: ID of the application to check
    
    Returns:
        List of created LandConflict objects
    """
    created_conflicts = []
    
    try:
        logger.info(f"Starting duplicate detection for application {application_id}")
        
        # Get the application
        application = db.session.get(LandApplication, application_id)
        if not application:
            logger.error(f"Application {application_id} not found")
            return created_conflicts
        
        # 1. Check for identity duplicates - CHECK BOTH APPLICATIONS AND PARCELS
        if application.nrc_number:
            logger.info(f"Checking NRC {application.nrc_number} against applications and parcels")
            
            # A. Check against other applications
            identity_dups = LandApplication.query.filter(
                or_(
                    LandApplication.nrc_number == application.nrc_number,
                    LandApplication.tpin_number == application.tpin_number
                ),
                LandApplication.id != application_id,
                LandApplication.status != 'rejected'
            ).all()
            
            for dup_app in identity_dups:
                # Check if conflict already exists
                existing = LandConflict.query.filter_by(
                    application_id=application_id,
                    conflict_type='identity_duplicate'
                ).filter(
                    LandConflict.description.contains(dup_app.reference_number)
                ).first()
                
                if not existing:
                    conflict = LandConflict(
                        application_id=application_id,
                        conflicting_parcel_id=None,
                        description=f"⚠️ DUPLICATE NRC DETECTED\n\n"
                                   f"Your NRC number ({application.nrc_number}) has already been used in another application.\n\n"
                                   f"EXISTING APPLICATION:\n"
                                   f"  Reference: {dup_app.reference_number}\n"
                                   f"  Applicant Name: {dup_app.applicant_name}\n"
                                   f"  Location: {dup_app.land_location}\n"
                                   f"  Submitted: {dup_app.submitted_at.strftime('%Y-%m-%d')}\n\n"
                                   f"WHAT THIS MEANS:\n"
                                   f"  - You may have already applied for land registration\n"
                                   f"  - Someone may be using your NRC fraudulently\n"
                                   f"  - If this is your previous application, please contact us\n\n"
                                   f"REQUIRED ACTION:\n"
                                   f"  Contact the land registry immediately to verify your identity and resolve this issue.",
                        detected_by_ai=True,
                        conflict_type='identity_duplicate',
                        title=f"⚠️ Duplicate NRC: {application.nrc_number}",
                        severity='high',
                        confidence_score=0.95,
                        status='unresolved'
                    )
                    db.session.add(conflict)
                    created_conflicts.append(conflict)
                    logger.info(f"Identity duplicate found in applications: {dup_app.reference_number}")
            
            # B. Check against existing land parcels (IMPORTANT!)
            identity_parcels = LandParcel.query.filter(
                LandParcel.owner_nrc == application.nrc_number
            ).all()
            
            for parcel in identity_parcels:
                # Check if this parcel's application is different from current one
                if parcel.application_id and parcel.application_id != application_id:
                    continue  # Already handled in section A
                
                # Check if conflict already exists
                existing = LandConflict.query.filter_by(
                    application_id=application_id,
                    conflicting_parcel_id=parcel.id,
                    conflict_type='identity_duplicate'
                ).first()
                
                if not existing:
                    conflict = LandConflict(
                        application_id=application_id,
                        conflicting_parcel_id=parcel.id,
                        description=f"⚠️ NRC ALREADY REGISTERED\n\n"
                                   f"Your NRC number ({application.nrc_number}) is already registered to an existing land parcel.\n\n"
                                   f"EXISTING PARCEL:\n"
                                   f"  Parcel Number: {parcel.parcel_number}\n"
                                   f"  Owner Name: {parcel.owner_name}\n"
                                   f"  Location: {parcel.location}\n"
                                   f"  Size: {parcel.size} hectares\n"
                                   f"  Certificate: {parcel.certificate_number or 'N/A'}\n\n"
                                   f"WHAT THIS MEANS:\n"
                                   f"  - You already own registered land\n"
                                   f"  - You may be applying for additional land (if legitimate)\n"
                                   f"  - Someone may be using your NRC fraudulently\n\n"
                                   f"REQUIRED ACTION:\n"
                                   f"  Provide written justification for additional land registration, or contact us if this is fraudulent.",
                        detected_by_ai=True,
                        conflict_type='identity_duplicate',
                        title=f"⚠️ NRC Already Owns Land: {parcel.parcel_number}",
                        severity='high',
                        confidence_score=0.95,
                        status='unresolved'
                    )
                    db.session.add(conflict)
                    created_conflicts.append(conflict)
                    logger.info(f"Identity duplicate found in parcels: {parcel.parcel_number}")
        
        # 2. Check for document hash duplicates (CRITICAL for fraud detection)
        app_docs = Document.query.filter_by(application_id=application_id).all()
        logger.info(f"Checking {len(app_docs)} documents for duplicates")
        
        for doc in app_docs:
            if not doc.file_hash:
                logger.warning(f"Document {doc.id} has no hash - cannot check for duplicates")
                continue
            
            # Find other documents with same hash
            dup_docs = Document.query.filter(
                Document.file_hash == doc.file_hash,
                Document.application_id != application_id
            ).all()
            
            logger.info(f"Found {len(dup_docs)} duplicate documents for {doc.document_type}")
            
            for dup_doc in dup_docs:
                dup_app = dup_doc.application
                
                # Check if conflict already exists for THIS specific document pair
                existing = LandConflict.query.filter_by(
                    application_id=application_id,
                    conflict_type='document_duplicate'
                ).filter(
                    LandConflict.description.contains(dup_app.reference_number),
                    LandConflict.description.contains(doc.document_type)
                ).first()
                
                if not existing:
                    # Determine if names match (legitimate) or differ (fraud)
                    name_matches = (application.applicant_name.lower().strip() == 
                                  dup_app.applicant_name.lower().strip())
                    
                    if name_matches:
                        severity_level = 'medium'
                        fraud_indicator = "DUPLICATE APPLICATION (Same Person)"
                        action_required = "This appears to be a duplicate application. Please confirm if this is intentional."
                    else:
                        severity_level = 'high'
                        fraud_indicator = "DOCUMENT FRAUD DETECTED (Different People)"
                        action_required = "This is a serious issue. The same document is being used by different applicants. Immediate verification required."
                    
                    conflict = LandConflict(
                        application_id=application_id,
                        conflicting_parcel_id=None,
                        description=f"⚠️ {fraud_indicator}\n\n"
                                   f"Your document '{doc.document_type}' ({doc.original_filename}) is IDENTICAL to a document from another application.\n\n"
                                   f"YOUR APPLICATION:\n"
                                   f"  Applicant: {application.applicant_name}\n"
                                   f"  NRC: {application.nrc_number}\n"
                                   f"  Document: {doc.document_type}\n\n"
                                   f"MATCHING DOCUMENT FROM:\n"
                                   f"  Reference: {dup_app.reference_number}\n"
                                   f"  Applicant: {dup_app.applicant_name}\n"
                                   f"  NRC: {dup_app.nrc_number}\n"
                                   f"  Location: {dup_app.land_location}\n"
                                   f"  Submitted: {dup_app.submitted_at.strftime('%Y-%m-%d')}\n"
                                   f"  Document: {dup_doc.document_type}\n\n"
                                   f"WHAT THIS MEANS:\n"
                                   f"  - The exact same file was uploaded for both applications\n"
                                   f"  - This indicates either document reuse or fraudulent submission\n"
                                   f"  - Legitimate documents should be unique to each applicant\n\n"
                                   f"REQUIRED ACTION:\n"
                                   f"  {action_required}",
                        detected_by_ai=True,
                        conflict_type='document_duplicate',
                        title=f"⚠️ Document Reuse: {doc.document_type}",
                        severity=severity_level,
                        confidence_score=0.98,  # Hash match = very high confidence
                        status='unresolved'
                    )
                    db.session.add(conflict)
                    created_conflicts.append(conflict)
                    logger.info(f"Document duplicate found: {doc.document_type} matches {dup_app.reference_number} (Names match: {name_matches})")
                    break  # Only create one conflict per document type
        
        # 3. Location duplicate check removed - handled by ai_conflict_clean.py with improved plot number matching
        
        # Update application duplicate score
        if created_conflicts:
            max_confidence = max(c.confidence_score for c in created_conflicts)
            application.ai_duplicate_score = max_confidence
            if application.status == 'pending':
                application.status = 'conflict'
        else:
            application.ai_duplicate_score = 0.0
        
        # Commit changes
        db.session.commit()
        
        # Log audit
        try:
            audit_log = AuditLog(
                user_id=None,
                action='detect_duplicates',
                table_name='land_applications',
                record_id=application_id,
                new_values={'duplicates_found': len(created_conflicts)},
                timestamp=datetime.utcnow()
            )
            db.session.add(audit_log)
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to log audit: {e}")
        
        logger.info(f"Duplicate detection complete for application {application_id}: {len(created_conflicts)} duplicates")
        return created_conflicts
    
    except Exception as e:
        logger.exception(f"Error in duplicate detection for application {application_id}")
        db.session.rollback()
        return created_conflicts


def resolve_duplicate(conflict_id: int, resolved_by: int = None) -> bool:
    """
    Mark a duplicate conflict as resolved.
    
    Args:
        conflict_id: ID of the conflict to resolve
        resolved_by: User ID who resolved it
    
    Returns:
        True if successful, False otherwise
    """
    try:
        conflict = db.session.get(LandConflict, conflict_id)
        if not conflict:
            return False
        
        conflict.status = 'resolved'
        conflict.resolved_at = datetime.utcnow()
        db.session.commit()
        
        # Log audit
        try:
            audit_log = AuditLog(
                user_id=resolved_by,
                action='resolve_duplicate',
                table_name='land_conflicts',
                record_id=conflict_id,
                old_values={'status': 'unresolved'},
                new_values={'status': 'resolved'},
                timestamp=datetime.utcnow()
            )
            db.session.add(audit_log)
            db.session.commit()
        except Exception as e:
            logger.error(f"Failed to log resolution: {e}")
        
        logger.info(f"Duplicate conflict {conflict_id} resolved")
        return True
    
    except Exception as e:
        logger.exception(f"Error resolving duplicate conflict {conflict_id}")
        db.session.rollback()
        return False
