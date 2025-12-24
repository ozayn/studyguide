# Google OAuth Setup Guide

This guide will help you set up Google OAuth credentials for admin authentication.

## Step-by-Step Instructions

### 1. Go to Google Cloud Console
- Visit: https://console.cloud.google.com/
- Sign in with your Google account

### 2. Create a New Project (if you don't have one)
- Click the project dropdown at the top
- Click "New Project"
- Enter a project name (e.g., "My Study Guide")
- Click "Create"

### 3. Enable OAuth Consent Screen
- Go to **APIs & Services** > **OAuth consent screen**
- Select **External** (for public apps) or **Internal** (if you have Google Workspace)
- Click "Create"

**Fill in the required information:**
- **App name**: My Study Guide (or your preferred name)
- **User support email**: Your email
- **Developer contact information**: Your email
- Click "Save and Continue"

**Scopes** (Step 2):
- Click "Add or Remove Scopes"
- Select these scopes:
  - `openid`
  - `.../auth/userinfo.email`
  - `.../auth/userinfo.profile`
- Click "Update" then "Save and Continue"

**Test users** (Step 3 - if External):
- Add your email address as a test user
- Click "Save and Continue"

**Summary** (Step 4):
- Review and click "Back to Dashboard"

### 4. Create OAuth 2.0 Credentials
- Go to **APIs & Services** > **Credentials**
- Click **"+ CREATE CREDENTIALS"** at the top
- Select **"OAuth client ID"**

**Application type**: Select **"Web application"**

**Name**: Enter a name (e.g., "My Study Guide Web Client")

**Authorized JavaScript origins**:
- For local development: `http://localhost:5006`
- For Railway production: `https://your-app-name.railway.app`
- Click "+ ADD URI" for each

**Authorized redirect URIs**:
- For local development: `http://localhost:5006/auth/callback`
- For Railway production: `https://your-app-name.railway.app/auth/callback`
- Click "+ ADD URI" for each

**Click "CREATE"**

### 5. Copy Your Credentials
After creation, a dialog will show:
- **Your Client ID**: `xxxxx.apps.googleusercontent.com` → This is your `GOOGLE_CLIENT_ID`
- **Your Client Secret**: `xxxxx` → This is your `GOOGLE_CLIENT_SECRET`

**⚠️ Important**: Copy these immediately - you won't be able to see the secret again!

### 6. Add to Your `.env` File
Add these to your `.env` file:

```bash
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxx
ADMIN_EMAILS=your-email@example.com
SECRET_KEY=generate-a-random-secret-key-here
```

**For `SECRET_KEY`**: Generate a random string (you can use Python):
```python
import secrets
print(secrets.token_hex(32))
```

### 7. For Railway Deployment
When you deploy to Railway:
1. Add the same environment variables in Railway's dashboard
2. Make sure the **Authorized redirect URI** in Google Cloud Console matches your Railway domain:
   - `https://your-app-name.railway.app/auth/callback`

## Troubleshooting

**"Redirect URI mismatch" error:**
- Make sure the redirect URI in Google Cloud Console exactly matches your app's URL + `/auth/callback`
- Check for trailing slashes, http vs https, etc.

**"Access blocked" error:**
- If using External app type, make sure your email is added as a test user
- The app might need to be published if you want to use it beyond test users

**Local development not working:**
- Make sure `http://localhost:5006/auth/callback` is in the Authorized redirect URIs
- The app bypasses authentication on localhost by default (see `app.py`)

## Security Notes

- Never commit your `GOOGLE_CLIENT_SECRET` to git
- Keep your `.env` file in `.gitignore` (already done)
- Use different credentials for development and production if needed
- Rotate secrets if they're ever exposed




