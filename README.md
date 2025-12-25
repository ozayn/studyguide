# My Study Guide

A web application to help you prepare for interviews by tracking your study topics and generating personalized study plans.

## Features

- **Interview Management**: Create and track multiple interviews with dates
- **Topic Tracking**: Add study topics with priorities and notes
- **AI-Powered Study Guidance**: Get AI-generated guidance on the minimum essential knowledge needed for each topic based on your position
- **Study Plan Generation**: Automatically generates a daily study schedule based on your interview date and topics
- **Progress Tracking**: Mark topics as completed and track your progress

## Setup

1. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. (Optional) Set up AI Guidance - Choose one:
   
   **Option A: Groq (Recommended - Fast & Free)**
   - Get a free API key from [Groq Console](https://console.groq.com)
   - Generous free tier, very fast responses
   
   **Option B: Google Gemini (Also Free)**
   - Get a free API key from [Google AI Studio](https://makersuite.google.com/app/apikey)
   - 60 requests per minute free tier
   
   **API keys are stored in `.env` file** (this file is git-ignored for security).
   
   Create a `.env` file in the project root:
   ```bash
   # .env file
   GROQ_API_KEY=your-groq-api-key-here
   # OR
   GOOGLE_API_KEY=your-google-api-key-here
   ```
   
   The app will automatically load these variables from `.env` using `python-dotenv`.
   
   **Note:** Your current Groq API key is already saved in the `.env` file - no need to set it up again!
   
   The AI guidance feature will automatically use whichever API key is available (Groq is tried first)

4. (Optional) Set up Admin Authentication for `/admin` page:
   
   **Google OAuth (Required for production deployment)**
   - Get OAuth credentials from [Google Cloud Console](https://console.cloud.google.com/)
   - Create OAuth 2.0 Client ID credentials
   - Add authorized redirect URI: `https://your-domain.com/auth/callback` (or `http://localhost:5006/auth/callback` for local)
   
   Add to your `.env` file:
   ```bash
   # Admin Authentication
   GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-google-client-secret
   ADMIN_EMAILS=your-email@example.com,another-admin@example.com
   SECRET_KEY=your-secret-key-for-sessions
   ```
   
   **Note:** For local development, authentication is bypassed when running on localhost. For production (Railway), authentication is required.

4. Run the application:
```bash
python app.py
```

5. Open your browser and navigate to `http://localhost:5006`

## Usage

1. **Create an Interview**: Fill in the company, position, and interview date
2. **Add Topics**: For each interview, add the topics you need to study
3. **Get AI Guidance**: Click "✨ AI Guide" on any topic to get AI-powered guidance on the minimum essential knowledge needed for that topic based on your position
4. **Set Priorities**: Mark topics as high, medium, or low priority
5. **Generate Study Plan**: Click "Generate Plan" to get a daily schedule
6. **Track Progress**: Mark topics as completed as you study them

The study plan automatically distributes your topics across the days leading up to your interview, prioritizing high-priority topics first.

## Railway Deployment

This application is configured for deployment on Railway. The following files are required:

- **Procfile**: Defines how Railway runs the application (using gunicorn)
- **runtime.txt**: Specifies Python version (3.12.0)
- **requirements.txt**: Includes gunicorn for production server

### Railway Setup Steps

1. **Create a Railway Project**:
   - Connect your GitHub repository to Railway
   - Railway will automatically detect the Flask app

2. **Add PostgreSQL Database**:
   - In Railway dashboard, click **"+ New"** → **"Database"** → **"Add PostgreSQL"**
   - Railway automatically creates the database and sets `DATABASE_URL` environment variable
   - The app will automatically use PostgreSQL when `DATABASE_URL` is present

3. **Set Environment Variables** in Railway dashboard:
   ```
   GROQ_API_KEY=your-groq-api-key
   GOOGLE_CLIENT_ID=your-google-client-id
   GOOGLE_CLIENT_SECRET=your-google-client-secret
   ADMIN_EMAILS=your-email@example.com
   SECRET_KEY=your-secret-key-for-sessions
   RAILWAY_ENVIRONMENT=1
   ```
   **Note:** `DATABASE_URL` is automatically set by Railway when you add PostgreSQL - no need to set it manually!

4. **Configure OAuth Redirect URI**:
   - In Google Cloud Console, add your Railway domain as an authorized redirect URI:
     `https://your-app-name.railway.app/auth/callback`

5. **Deploy**:
   - Railway will automatically deploy on git push
   - The app will be available at `https://your-app-name.railway.app`

### Local vs Production

- **Local Development**: 
  - Runs on port 5006 with debug mode enabled
  - Uses SQLite database (`interview_prep.db`)
  - Authentication bypassed on localhost
  
- **Railway Production**: 
  - Uses gunicorn with 2 workers, listens on Railway's PORT environment variable
  - Uses PostgreSQL database (automatically configured via `DATABASE_URL`)
  - Authentication required (Google OAuth)
  - Database schema is automatically initialized on first run

### Database

The app supports both SQLite (local) and PostgreSQL (Railway):
- **SQLite**: Used automatically when `DATABASE_URL` is not set (local development)
- **PostgreSQL**: Used automatically when `DATABASE_URL` is set (Railway production)
- No code changes needed - the app detects and uses the appropriate database

See `RAILWAY_DATABASE_SETUP.md` for detailed database setup instructions.

## Google Drive Study Material (Local-only)

This project includes an **optional Google Drive integration** to generate study notes and flashcards from your Drive course materials:

- **Supported inputs**: PDFs and `.ipynb` notebooks (data files like `.csv`, `.parquet`, `.zip`, images are ignored)
- **Single-file workflow** (recommended for iteration):
  - Paste a Drive **file link** into Admin → Drive ID
  - Generate **Deep dive notes** (single version) and then **flashcards derived from those notes**
- **Folder workflow** (for later, after single-file is dialed in):
  - Index → extract → generate folder-scoped guides/flashcards

Security / deployment:
- Drive features are intended to be **local-only** unless explicitly enabled via environment flags (to avoid exposing Drive OAuth in production).

## Status / Next steps

High-priority:
- [ ] **Push latest changes to GitHub**
- [ ] **Confirm production safety**: Drive UI/Drive endpoints remain disabled unless explicitly enabled (and only admins can access `/admin`)
- [ ] **Verify single-file Deep dive quality** across a few different notebook types (EDA, modeling, CV/NLP, time-series)

Quality improvements (single-file, then apply to folder mode):
- [ ] **Tune extraction for `.ipynb`**: ensure we capture enough context for code workflows, common plots, and regularization (when present)
- [ ] **Improve “Deep dive” completeness**: confirm it always includes everything the concise base would include (2-pass expand)
- [ ] **Flashcards sanity pass**: confirm all cards are grounded in notes with evidence and remove any low-quality/duplicative cards

Folder mode (after single-file is solid):
- [ ] Apply the same deep-dive notes + notes-derived flashcards pipeline to **folder processing**
- [ ] Validate folder scoping (no cross-folder mixing) and progress/ETA behavior on large folders

