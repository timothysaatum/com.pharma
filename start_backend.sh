#!/bin/bash

# Kill any existing backend processes
pkill -f "uvicorn.*main:app" || true
pkill -f "python.*main\.py" || true

# Wait for ports to free up
sleep 2

# Navigate to backend directory and start the server
cd /home/vermithor/Desktop/inventory/com.pharma/backend.laso

# Activate environment
source /home/vermithor/lasoenv/bin/activate

# Start backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
