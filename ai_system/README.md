# OjoIA AI Surveillance System

## Architecture

### Services
- **Qwen2.5-VL-7B** (port 8004) - Vision AI for image analysis
- **Whisper** (port 8008) - Audio transcription  
- **SDXL** (port 8006) - Image generation
- **YOLOv8** (port 8002) - Person/object detection
- **API Eva** (port 8005) - Main API gateway

### Directory Structure
```
/mnt/storage/
└── users/
    └── {user_id}/
        └── user.json        # User registration data
/opt/ai_models/              # AI models
/home/sam/ai_system/         # Source code
```

## API Endpoints

### Core
- `GET /health` - Health check
- `POST /frames/ingest?camera_id=&user_id=` - ESP32 CAM frame ingestion
- `POST /grid/analyze` - Analyze full grid with Qwen
- `GET /grid/status` - Grid status (frame_count, camera_ids)

### Auth
- `POST /auth/firebase/verify` - Register/login user (creates user.json)

### User Data
- `GET /admin/users/{user_id}` - Get user details from user.json
- `POST /admin/storage/{user_id}/migrate` - Assign disk to user

## User Registration Flow
1. User registers in app-v5.js
2. Firebase token verified by `/auth/firebase/verify`
3. User JSON created at `/mnt/storage/users/{uid}/user.json`
4. Admin assigns disk/location via Storage panel

## Start Services
```bash
./start-all.sh
```

## Systemd Service
```bash
sudo cp ojoia.service /etc/systemd/system/
sudo systemctl enable ojoia
sudo systemctl start ojoia
```