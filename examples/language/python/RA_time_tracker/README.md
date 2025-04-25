# Time Tracking Application

A full-stack application to replace Google Sheets for tracking work hours across different categories and resources.

## Tech Stack
- **Frontend**: React.js with TypeScript
- **Backend**: Node.js with Express
- **Database**: PostgreSQL
- **Authentication**: JWT-based auth system
- **Deployment**: Docker containers

## Setup Options

> **Note:** If you encounter any issues with these setup methods, please see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for help.

### Option 1: Run PostgreSQL in Docker, Apps locally

#### Prerequisites
- Docker or Podman
- Node.js (v14+)

#### Starting the Application
1. Clone this repository
2. Start PostgreSQL in Docker:
   ```
   docker-compose -f docker-compose.dev.yml up -d
   ```
   or with Podman:
   ```
   podman compose -f docker-compose.dev.yml up -d
   ```
3. Start the backend:
   ```
   cd backend
   npm install
   npm run dev
   ```
4. Start the frontend in a new terminal:
   ```
   cd frontend
   npm install
   npm start
   ```
5. Access the application at `http://localhost:3000`
6. Use the following credentials to log in:
   - Username: `admin`
   - Password: `admin123`

### Option 2: Full Docker Setup (when Docker is working)

#### Prerequisites
- Docker and Docker Compose

#### Starting the Application
1. Run `docker-compose up -d`
2. Access the application at `http://localhost:3000`

### Accessing Services
- Backend API: `http://localhost:5000`
- Frontend: `http://localhost:3000`
- Database: Accessible on port `5432`

## Manual Setup (No Docker)

### Prerequisites
- Node.js (v14+)
- PostgreSQL (installed locally)

### Installation Steps
1. Clone this repository
2. Set up the database:
   - Create a PostgreSQL database named `time_tracker`
   ```sql
   CREATE DATABASE time_tracker;
   ```
   - From the backend directory, run the database setup script:
   ```bash
   cd backend
   npm install
   npm run setup-db
   ```
3. Start the backend:
   - From the backend directory:
   ```bash
   npm run dev
   ```
4. Set up the frontend:
   - In a new terminal, navigate to the frontend directory:
   ```bash
   cd frontend
   npm install
   npm start
   ```

This will create the database schema and seed initial data, including an admin user with username `admin` and password `admin123`.

## Development

### Backend API Endpoints
- Auth: `POST /api/auth/login`, `POST /api/auth/register`
- Months: `GET /api/months`, `POST /api/months/create`, `GET /api/months/:id`

### Project Structure
- `backend/`: Express API server
- `frontend/`: React application
- `database/`: Database scripts and migrations