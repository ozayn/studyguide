# Railway Database Setup Guide

This guide explains how to set up the database for Railway deployment.

## Current Setup

The app currently uses **SQLite** (`interview_prep.db`) for local development. For Railway deployment, you have two options:

## Option 1: PostgreSQL (Recommended for Production) ⭐

PostgreSQL is recommended because:
- ✅ Data persists across redeploys automatically
- ✅ Better performance for production
- ✅ Railway provides managed PostgreSQL service
- ✅ More robust and scalable
- ✅ Railway handles backups automatically

### Steps to Set Up PostgreSQL on Railway

1. **Add PostgreSQL Service in Railway**:
   - In your Railway project dashboard, click **"+ New"**
   - Select **"Database"** → **"Add PostgreSQL"**
   - Railway will automatically create a PostgreSQL database

2. **Railway Auto-Configures Environment Variables**:
   - Railway automatically adds `DATABASE_URL` to your web service
   - The format is: `postgresql://user:password@host:port/dbname`
   - **No manual configuration needed!**

3. **Update Your Code**:
   - The app needs to be updated to support PostgreSQL
   - See "Code Updates Required" section below

4. **Deploy**:
   - Push your code to GitHub
   - Railway will automatically deploy
   - The database will be initialized on first run

## Option 2: SQLite with Persistent Volume (Simpler, but Limited)

If you want to keep SQLite without code changes:

1. **Add Volume in Railway**:
   - In your Railway project, click **"+ New"**
   - Select **"Volume"**
   - Mount it to `/data` in your web service

2. **Update Database Path in Code**:
   - Change `DATABASE = 'interview_prep.db'` to `DATABASE = '/data/interview_prep.db'`

3. **Limitations**:
   - ⚠️ SQLite is not ideal for production (concurrent writes can be slow)
   - ⚠️ Data is tied to the volume (backup/restore is manual)
   - ⚠️ Not recommended for multiple instances

## Code Updates Required for PostgreSQL

The app needs to be updated to support both SQLite (local) and PostgreSQL (Railway). Here's what needs to change:

### 1. Add PostgreSQL Support to `requirements.txt`:
```
psycopg2-binary==2.9.9
```

### 2. Update `app.py` Database Functions:

The code needs to:
- Detect `DATABASE_URL` environment variable (Railway provides this)
- Use PostgreSQL if `DATABASE_URL` exists, otherwise use SQLite
- Handle SQL syntax differences between SQLite and PostgreSQL

**Key Changes Needed:**
- `get_db()` function needs to handle both database types
- `init_db()` needs to use PostgreSQL-compatible SQL
- `AUTOINCREMENT` → `SERIAL` or `GENERATED ALWAYS AS IDENTITY` for PostgreSQL
- Connection handling differs between sqlite3 and psycopg2

### 3. SQL Syntax Differences:

Most SQLite syntax works with PostgreSQL, but these may need adjustment:
- `AUTOINCREMENT` → `SERIAL` or `GENERATED ALWAYS AS IDENTITY` (PostgreSQL)
- `TEXT` works in both (no change needed)
- Date/time functions may differ slightly

## Recommended Approach

**For Production**: Use PostgreSQL (Option 1)
- More reliable
- Better performance  
- Data persists automatically
- Railway manages backups

**For Local Development**: Keep SQLite
- Simpler setup
- No database server needed
- Fast for development

## Quick Start: PostgreSQL Setup

1. **In Railway Dashboard**:
   - Add PostgreSQL database service
   - Railway automatically connects it to your web service
   - `DATABASE_URL` is automatically set

2. **Update Code** (see below for implementation):
   - Add `psycopg2-binary` to `requirements.txt`
   - Update `app.py` to support both databases

3. **Deploy**:
   - Push to GitHub
   - Railway deploys automatically
   - Database initializes on first run

## Environment Variables

Railway automatically provides:
- `DATABASE_URL` - Full PostgreSQL connection string
- Format: `postgresql://user:password@host:port/dbname`

Your app should check for this variable and use PostgreSQL if present, otherwise use SQLite.

## Testing Locally with PostgreSQL

If you want to test PostgreSQL locally before deploying:

1. **Install PostgreSQL locally or use Docker**:
   ```bash
   docker run --name postgres-test -e POSTGRES_PASSWORD=test -p 5432:5432 -d postgres
   ```

2. **Set `DATABASE_URL` in your `.env`**:
   ```
   DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres
   ```

3. **Run your app** - it should connect to PostgreSQL

## Migration from SQLite to PostgreSQL

If you have existing data in SQLite:

1. Export data from SQLite (using SQLite tools or Python script)
2. Import into PostgreSQL (using `psql` or Python script)
3. Verify data integrity

**Or** simply start fresh on Railway - the app will initialize the database schema automatically on first run.

## Next Steps

Would you like me to:
1. Update the code to support PostgreSQL (recommended)?
2. Or keep SQLite and use a persistent volume (simpler, but less ideal)?

