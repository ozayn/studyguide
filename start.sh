#!/bin/bash
# Note: Environment variables are now loaded from .env file via python-dotenv
# No need to source setup_env.sh or export keys manually

source venv/bin/activate
python app.py
