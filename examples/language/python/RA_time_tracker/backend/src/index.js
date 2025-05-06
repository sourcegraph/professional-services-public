require('dotenv').config();
const express = require('express');
const cors = require('cors');
const db = require('./models');

// Import routes
const authRoutes = require('./routes/auth.routes');
const monthRoutes = require('./routes/month.routes');

// Initialize Express app
const app = express();
const PORT = process.env.PORT || 5000;

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Health check endpoint
app.get('/', (req, res) => {
  res.json({ message: 'Time Tracker API is running' });
});

// Import additional routes
const resourceRoutes = require('./routes/resource.routes');
const categoryRoutes = require('./routes/category.routes');
const timeEntryRoutes = require('./routes/timeEntry.routes');

// Routes
app.use('/api/auth', authRoutes);
app.use('/api/months', monthRoutes);
app.use('/api/resources', resourceRoutes);
app.use('/api/categories', categoryRoutes);
app.use('/api/timeentries', timeEntryRoutes);

// Database connection and synchronization with retry logic
const connectWithRetry = async () => {
  try {
    console.log('Attempting to connect to the database...');
    await db.sequelize.authenticate();
    console.log('Database connection established successfully.');
    
    // Sync database models without altering existing tables
    await db.sequelize.sync({ force: false, alter: false });
    console.log('Database synchronized');
    
    // Start server
    app.listen(PORT, () => {
      console.log(`Server is running on port ${PORT}`);
    });
  } catch (error) {
    console.error('Unable to connect to the database:', error);
    console.log('Retrying in 5 seconds...');
    setTimeout(connectWithRetry, 5000);
  }
};

// Initial connection attempt
connectWithRetry();

// Error handling middleware
app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).send('Something broke!');
});