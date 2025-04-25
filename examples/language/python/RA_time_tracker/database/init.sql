-- Create tables first

-- Create role enum type
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'enum_users_role') THEN
    CREATE TYPE enum_users_role AS ENUM ('admin', 'manager', 'user');
  END IF;
END
$$;

-- Users Table
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username VARCHAR(255) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  role enum_users_role DEFAULT 'user',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Resources Table
CREATE TABLE IF NOT EXISTS resources (
  id SERIAL PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  active_status BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Categories Table
CREATE TABLE IF NOT EXISTS categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  description TEXT,
  active_status BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Months Table
CREATE TABLE IF NOT EXISTS months (
  id SERIAL PRIMARY KEY,
  year INTEGER NOT NULL,
  month INTEGER NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(year, month)
);

-- Weeks Table
CREATE TABLE IF NOT EXISTS weeks (
  id SERIAL PRIMARY KEY,
  month_id INTEGER NOT NULL REFERENCES months(id),
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  week_number INTEGER NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- TimeEntries Table
CREATE TABLE IF NOT EXISTS time_entries (
  id SERIAL PRIMARY KEY,
  resource_id INTEGER NOT NULL REFERENCES resources(id),
  category_id INTEGER NOT NULL REFERENCES categories(id),
  week_id INTEGER NOT NULL REFERENCES weeks(id),
  hours DECIMAL(10, 2) NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create initial admin user
INSERT INTO users (username, email, password_hash, role, created_at, updated_at)
VALUES (
  'admin',
  'admin@example.com',
  -- This is a hashed version of 'admin123' - you should change this in production
  '$2b$10$BKeuit/UeOJydni8mTrRE.YevsVxuMTnQE1y.9NwJyxuLI27lz186',
  'admin',
  NOW(),
  NOW()
) ON CONFLICT (username) DO NOTHING;

-- Create sample resource
INSERT INTO resources (name, active_status, created_at, updated_at)
VALUES ('Resident Architect', true, NOW(), NOW()),
       ('Moshin Jaffar', true, NOW(), NOW())
ON CONFLICT DO NOTHING;

-- Create sample categories
INSERT INTO categories (name, description, active_status, created_at, updated_at)
VALUES ('Custom RA Initiatives', 'Custom Resident Architect initiatives and tasks', true, NOW(), NOW()),
       ('Project Planning & Coordination', 'Planning and coordination activities', true, NOW(), NOW()),
       ('Design / Build Work / Support', 'Design, implementation and support work', true, NOW(), NOW()),
       ('User Training', 'Training provided to users', true, NOW(), NOW()),
       ('Internal / External Meetings', 'Internal and external meetings', true, NOW(), NOW())
ON CONFLICT DO NOTHING;