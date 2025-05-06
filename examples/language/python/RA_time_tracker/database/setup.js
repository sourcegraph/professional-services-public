/**
 * Database setup script - Run this to create tables and seed initial data
 * 
 * Usage: 
 * 1. Ensure PostgreSQL is running
 * 2. Create a database named 'time_tracker'
 * 3. Run: node setup.js
 */

const { Sequelize } = require('sequelize');
const bcrypt = require('bcrypt');

// Database connection
const sequelize = new Sequelize('time_tracker', 'postgres', 'postgres', {
  host: 'localhost',
  dialect: 'postgres',
  logging: console.log
});

// Define models
const User = sequelize.define('User', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  username: {
    type: Sequelize.STRING,
    allowNull: false,
    unique: true
  },
  password_hash: {
    type: Sequelize.STRING,
    allowNull: false
  },
  email: {
    type: Sequelize.STRING,
    allowNull: false,
    unique: true
  },
  role: {
    type: Sequelize.ENUM('admin', 'manager', 'user'),
    defaultValue: 'user'
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'users',
  timestamps: true,
  underscored: true
});

const Resource = sequelize.define('Resource', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  name: {
    type: Sequelize.STRING,
    allowNull: false
  },
  active_status: {
    type: Sequelize.BOOLEAN,
    defaultValue: true
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'resources',
  timestamps: true,
  underscored: true
});

const Category = sequelize.define('Category', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  name: {
    type: Sequelize.STRING,
    allowNull: false
  },
  description: {
    type: Sequelize.TEXT
  },
  active_status: {
    type: Sequelize.BOOLEAN,
    defaultValue: true
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'categories',
  timestamps: true,
  underscored: true
});

const Month = sequelize.define('Month', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  year: {
    type: Sequelize.INTEGER,
    allowNull: false
  },
  month: {
    type: Sequelize.INTEGER,
    allowNull: false
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'months',
  timestamps: true,
  underscored: true,
  indexes: [
    {
      unique: true,
      fields: ['year', 'month']
    }
  ]
});

const Week = sequelize.define('Week', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  month_id: {
    type: Sequelize.INTEGER,
    allowNull: false,
    references: {
      model: 'months',
      key: 'id'
    }
  },
  start_date: {
    type: Sequelize.DATEONLY,
    allowNull: false
  },
  end_date: {
    type: Sequelize.DATEONLY,
    allowNull: false
  },
  week_number: {
    type: Sequelize.INTEGER,
    allowNull: false
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'weeks',
  timestamps: true,
  underscored: true
});

const TimeEntry = sequelize.define('TimeEntry', {
  id: {
    type: Sequelize.INTEGER,
    primaryKey: true,
    autoIncrement: true
  },
  resource_id: {
    type: Sequelize.INTEGER,
    allowNull: false,
    references: {
      model: 'resources',
      key: 'id'
    }
  },
  category_id: {
    type: Sequelize.INTEGER,
    allowNull: false,
    references: {
      model: 'categories',
      key: 'id'
    }
  },
  week_id: {
    type: Sequelize.INTEGER,
    allowNull: false,
    references: {
      model: 'weeks',
      key: 'id'
    }
  },
  hours: {
    type: Sequelize.DECIMAL(10, 2),
    allowNull: false
  },
  created_by: {
    type: Sequelize.INTEGER,
    references: {
      model: 'users',
      key: 'id'
    }
  },
  created_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  },
  updated_at: {
    type: Sequelize.DATE,
    defaultValue: Sequelize.NOW
  }
}, {
  tableName: 'time_entries',
  timestamps: true,
  underscored: true
});

// Setup associations
Month.hasMany(Week, { as: 'weeks', foreignKey: 'month_id' });
Week.belongsTo(Month, { foreignKey: 'month_id' });

Week.hasMany(TimeEntry, { as: 'timeEntries', foreignKey: 'week_id' });
TimeEntry.belongsTo(Week, { foreignKey: 'week_id' });

Resource.hasMany(TimeEntry, { as: 'timeEntries', foreignKey: 'resource_id' });
TimeEntry.belongsTo(Resource, { foreignKey: 'resource_id' });

Category.hasMany(TimeEntry, { as: 'timeEntries', foreignKey: 'category_id' });
TimeEntry.belongsTo(Category, { foreignKey: 'category_id' });

// Seed data function
async function seedData() {
  // Create admin user
  const salt = await bcrypt.genSalt(10);
  const passwordHash = await bcrypt.hash('admin123', salt);
  
  await User.create({
    username: 'admin',
    email: 'admin@example.com',
    password_hash: passwordHash,
    role: 'admin'
  });
  
  // Create sample resources
  await Resource.bulkCreate([
    { name: 'John Doe', active_status: true },
    { name: 'Jane Smith', active_status: true },
    { name: 'Bob Johnson', active_status: true }
  ]);
  
  // Create sample categories
  await Category.bulkCreate([
    { name: 'Development', description: 'Software development tasks', active_status: true },
    { name: 'Testing', description: 'Quality assurance and testing', active_status: true },
    { name: 'Design', description: 'UI/UX design work', active_status: true },
    { name: 'Project Management', description: 'Project coordination and management', active_status: true }
  ]);
  
  console.log('Initial data seeded successfully');
}

// Main function to set up database
async function setupDatabase() {
  try {
    // Test connection
    await sequelize.authenticate();
    console.log('Database connection established successfully.');
    
    // Sync all models (create tables)
    await sequelize.sync({ force: true });
    console.log('Database tables created');
    
    // Seed initial data
    await seedData();
    
    console.log('Database setup completed successfully');
  } catch (error) {
    console.error('Database setup failed:', error);
  } finally {
    await sequelize.close();
  }
}

// Run the setup
setupDatabase();