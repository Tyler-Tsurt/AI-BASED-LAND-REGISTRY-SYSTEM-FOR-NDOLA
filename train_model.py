import random
import pickle
import numpy as np
import pandas as pd
from difflib import SequenceMatcher
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from shapely import wkb
from app import app, db
from models import LandParcel

# --- Configuration ---
MODEL_PATH = 'models/conflict_classifier.pkl'
TRAINING_SIZE = 2000  # How many examples to generate

def similarity(a, b):
    """Returns text similarity between 0.0 and 1.0"""
    if not a or not b: return 0.0
    return SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio()

def generate_training_set(parcels):
    """
    Generates synthetic training data by mutating real parcels.
    Returns: X (Features), y (Labels)
    """
    data = []
    
    print(f"Generating {TRAINING_SIZE} training scenarios...")
    
    for _ in range(TRAINING_SIZE):
        # Pick a random target parcel from your real DB
        target = random.choice(parcels)
        
        # --- SCENARIO 1: CREATE A CONFLICT (Label = 1) ---
        if random.random() > 0.5:
            # Create an application that partially matches the target
            app_nrc = target.owner_nrc 
            app_location = target.location
            
            # 50% chance to slighty change location text (e.g., "Plot 1" -> "Plot 1A")
            if random.random() > 0.5:
                app_location = app_location + " EXT"
                
            # 50% chance to use different owner name (Identity Theft)
            app_name = target.owner_name if random.random() > 0.5 else "Fraudulent Applicant"
            
            label = 1 # CONFLICT
            
        # --- SCENARIO 2: CREATE A CLEAN APP (Label = 0) ---
        else:
            # Totally different data
            app_nrc = "999999/99/1"
            app_location = "Plot 99999, New Extension, Ndola"
            app_name = "New Citizen"
            label = 0 # CLEAN

        # --- FEATURE ENGINEERING ---
        # We calculate the features that the AI will look at
        
        # 1. Text Similarities
        loc_score = similarity(app_location, target.location)
        name_score = similarity(app_name, target.owner_name)
        
        # 2. ID Match (Binary)
        nrc_match = 1.0 if app_nrc == target.owner_nrc else 0.0
        
        # 3. Spatial Overlap (Simulated for training speed)
        # In the real app, you use PostGIS for this. 
        # Here we simulate: If it's a conflict, high overlap. If clean, 0 overlap.
        spatial_overlap = random.uniform(0.1, 1.0) if label == 1 else 0.0
        
        data.append([loc_score, name_score, nrc_match, spatial_overlap, label])

    # Convert to DataFrame
    df = pd.DataFrame(data, columns=['loc_score', 'name_score', 'nrc_match', 'spatial_overlap', 'label'])
    return df

def train_ai():
    with app.app_context():
        # 1. Fetch Real Data from DB
        print("Fetching real parcels from database...")
        parcels = LandParcel.query.limit(5000).all()
        
        if len(parcels) < 10:
            print("ERROR: Not enough data in database. Run generate_ndola_data.py first!")
            return

        # 2. Generate Training Data
        df = generate_training_set(parcels)
        
        # 3. Split Data
        X = df[['loc_score', 'name_score', 'nrc_match', 'spatial_overlap']]
        y = df['label']
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)
        
        # 4. Train Model (Random Forest)
        print("Training Random Forest Classifier...")
        clf = RandomForestClassifier(n_estimators=100, random_state=42)
        clf.fit(X_train, y_train)
        
        # 5. Validate
        print("\n--- Model Performance ---")
        print(f"Accuracy: {clf.score(X_test, y_test):.2%}")
        
        # 6. Save Model
        import os
        if not os.path.exists('models'):
            os.makedirs('models')
            
        with open(MODEL_PATH, 'wb') as f:
            pickle.dump(clf, f)
            
        print(f"\nModel saved to {MODEL_PATH}")
        print("The system is now trained to detect conflicts based on:")
        print("- Location Name Similarity")
        print("- Owner Name Similarity")
        print("- NRC Matches")
        print("- Spatial Overlaps")

if __name__ == "__main__":
    train_ai()