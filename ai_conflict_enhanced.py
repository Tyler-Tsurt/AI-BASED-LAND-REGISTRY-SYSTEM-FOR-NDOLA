"""
ai_conflict_enhanced.py
Optimized Document Analysis: Uses Hashing first, then scoped Text Analysis.
"""
import logging
import pickle
import os
from datetime import datetime
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from models import db, Document, LandApplication, LandConflict

logger = logging.getLogger(__name__)

def detect_conflicts_from_documents(application_id):
    created_conflicts = []
    try:
        app = LandApplication.query.get(application_id)
        if not app or not app.documents:
            return []

        # --- CHECK 1: EXACT DUPLICATES (Using File Hash) ---
        # Very fast, no AI needed
        for new_doc in app.documents:
            if not new_doc.file_hash:
                continue
                
            # Find ANY other document with same hash (SHA256)
            exact_dups = Document.query.filter(
                Document.file_hash == new_doc.file_hash,
                Document.application_id != application_id
            ).all()
            
            for dup in exact_dups:
                create_doc_conflict(
                    app, dup.application, new_doc, dup,
                    score=1.0, # 100% Match
                    reason="Exact file duplicate (Same Hash)",
                    created_list=created_conflicts
                )

        # --- CHECK 2: CONTENT SIMILARITY (Text Analysis) ---
        # Only run this for high-value documents (Title Deeds, Offer Letters)
        # Skip checking IDs/NRCs as they often have little extractable text or are images
        
        # Pre-load vectorizer (Singleton pattern ideally)
        vectorizer = TfidfVectorizer(stop_words='english')
        if os.path.exists('tfidf_vectorizer.pkl'):
            with open('tfidf_vectorizer.pkl', 'rb') as f:
                vectorizer = pickle.load(f)

        from document_processing import extract_document_text
        
        for new_doc in app.documents:
            # Only analyze text-heavy docs
            if new_doc.document_type not in ['Offer Letter', 'Title Deed', 'Affidavit']:
                continue
                
            new_text = extract_document_text(new_doc.file_path, new_doc.mime_type)
            if len(new_text) < 50: continue # Skip empty docs

            # OPTIMIZATION: Only compare against docs of the SAME TYPE
            # This prevents comparing a Title Deed to 5,000 NRC cards.
            candidates = Document.query.filter(
                Document.document_type == new_doc.document_type,
                Document.application_id != application_id,
                Document.id != new_doc.id
            ).limit(200).all() # Limit comparison to recent 200 docs for speed
            
            if not candidates: continue

            # Prepare Corpus
            candidate_texts = []
            valid_candidates = []
            
            for c in candidates:
                txt = extract_document_text(c.file_path, c.mime_type)
                if len(txt) > 50:
                    candidate_texts.append(txt)
                    valid_candidates.append(c)
            
            if not candidate_texts: continue
            
            # TF-IDF Comparison
            # Fit on local batch (faster than fitting whole DB)
            tfidf = TfidfVectorizer(stop_words='english').fit_transform([new_text] + candidate_texts)
            cosine_sim = cosine_similarity(tfidf[0:1], tfidf[1:])
            
            # Check results
            for idx, score in enumerate(cosine_sim[0]):
                if score > 0.85: # 85% Similarity Threshold
                    dup_doc = valid_candidates[idx]
                    create_doc_conflict(
                        app, dup_doc.application, new_doc, dup_doc,
                        score=float(score),
                        reason="High text content similarity",
                        created_list=created_conflicts
                    )

        if created_conflicts:
            app.status = 'conflict'
            db.session.commit()

        return created_conflicts

    except Exception as e:
        logger.exception(f"Doc analysis failed for App {application_id}")
        return []

def create_doc_conflict(app, other_app, new_doc, old_doc, score, reason, created_list):
    """Helper to save document conflicts"""
    desc = (f"⚠️ {reason}\n"
            f"Your document '{new_doc.original_filename}' is {score:.0%} similar to a document "
            f"in Application {other_app.reference_number} ({other_app.applicant_name}).\n"
            f"Document Type: {new_doc.document_type}")
            
    c = LandConflict(
        application_id=app.id,
        conflicting_parcel_id=None, # Document conflict, not parcel
        conflict_type='document_duplicate',
        confidence_score=score,
        title=f"Duplicate Document Detected ({score:.0%})",
        description=desc,
        severity='high',
        status='unresolved',
        detected_by_ai=True
    )
    db.session.add(c)
    created_list.append(c)