#!/bin/bash

# setup.sh - Setup script for web application
# Creates a virtual environment and installs requirements

set -e  # Exit on error

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Change to the script's directory
cd "$SCRIPT_DIR"

echo "🚀 Setting up web application..."
echo "📁 Working directory: $(pwd)"

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed. Please install Python 3 first."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    echo "✅ Virtual environment created successfully!"
else
    echo "ℹ️  Virtual environment already exists, skipping creation."
fi

# Activate virtual environment
echo "🔌 Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "⬆️  Upgrading pip..."
pip install --upgrade pip

# Install requirements if requirements.txt exists
if [ -f "requirements.txt" ]; then
    echo "📥 Installing requirements from requirements.txt..."
    pip install -r requirements.txt
    echo "✅ Requirements installed successfully!"
else
    echo "⚠️  Warning: requirements.txt not found. Skipping package installation."
    echo "   Please create a requirements.txt file with your dependencies."
fi

echo ""
echo "✨ Setup complete! You can now run ./run.sh to start the server."
