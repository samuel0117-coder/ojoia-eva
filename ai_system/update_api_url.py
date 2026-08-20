#!/usr/bin/env python3
"""Update Firestore with API URL for frontend to discover backend"""
import firebase_admin
from firebase_admin import credentials, firestore

try:
    firebase_admin.get_app()
except:
    firebase_admin.initialize_app(credentials.Certificate("/home/sam/Downloads/firebase-key.json"))

db = firestore.client()

# Update the server_status document
db.collection('system').document('server_status').set({
    'ngrok_url': 'https://api.ojoia.com.do',
    'status': 'online',
}, merge=True)

print("Updated API URL to https://api.ojoia.com.do in Firestore")