# Troubleshooting Guide

## Docker/Podman Issues

If you're experiencing issues with Docker or Podman, here are some steps to troubleshoot:

### 1. Check Docker/Podman Service Status

Make sure Docker or Podman is running:

```bash
# For Docker
docker info

# For Podman
podman info
```

If you see "connection refused" errors, it means the Docker daemon is not running or accessible.

### 2. Start Docker or Podman Service

```bash
# For Docker on Linux
sudo systemctl start docker

# For Docker Desktop on Mac/Windows
# Restart Docker Desktop from the application
```

### 3. Use Manual Setup Instead

If Docker/Podman issues persist, follow the manual setup instructions in the README.md file, which only requires a locally installed PostgreSQL database.

## Database Connection Issues

If the application can't connect to the database:

### 1. Verify PostgreSQL is Running

```bash
# Check status on Linux
sudo systemctl status postgresql

# Check on Mac with Homebrew
brew services list | grep postgresql
```

### 2. Check Connection Settings

Ensure your database connection settings match your local PostgreSQL configuration:

- Check the `.env` file in the backend directory
- Make sure username, password, and host are correct
- Default settings are:
  - Host: localhost
  - User: postgres
  - Password: postgres
  - Database: time_tracker

### 3. Create Database Manually

If needed, create the database manually:

```bash
psql -U postgres
> CREATE DATABASE time_tracker;
> \q
```

Then run the setup script:

```bash
cd backend
npm run setup-db
```

## Frontend Connection Issues

If the frontend can't connect to the backend:

### 1. Check Backend is Running

Make sure the backend service is running on port 5000:

```bash
curl http://localhost:5000
```

You should see a message indicating the API is running.

### 2. Check CORS Settings

If you get CORS errors in the browser console, make sure the backend is configured to allow requests from the frontend origin (http://localhost:3000).

### 3. Verify API URL

Check that the frontend is using the correct API URL:
- In development, it should be http://localhost:5000/api