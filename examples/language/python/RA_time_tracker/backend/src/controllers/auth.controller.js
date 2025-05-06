const bcrypt = require('bcrypt');
const jwt = require('jsonwebtoken');
const { User, Sequelize } = require('../models');
const { Op } = Sequelize;

/**
 * Authentication Controller
 */
class AuthController {
  /**
   * Register a new user
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async register(req, res) {
    try {
      const { username, email, password, role } = req.body;
      
      // Validate input
      if (!username || !email || !password) {
        return res.status(400).json({ message: 'Username, email, and password are required' });
      }
      
      // Check if username or email already exists
      const existingUser = await User.findOne({
        where: {
          [Op.or]: [
            { username },
            { email }
          ]
        }
      });
      
      if (existingUser) {
        return res.status(400).json({ message: 'Username or email already in use' });
      }
      
      // Hash password
      const salt = await bcrypt.genSalt(10);
      const passwordHash = await bcrypt.hash(password, salt);
      
      // Create user
      const newUser = await User.create({
        username,
        email,
        password_hash: passwordHash,
        role: role || 'user' // Default to 'user' if no role provided
      });
      
      // Return success but don't include password hash
      res.status(201).json({
        id: newUser.id,
        username: newUser.username,
        email: newUser.email,
        role: newUser.role
      });
    } catch (error) {
      console.error('Registration error:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred during registration.'
      });
    }
  }

  /**
   * Login a user
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async login(req, res) {
    try {
      const { username, password } = req.body;
      
      // Validate input
      if (!username || !password) {
        return res.status(400).json({ message: 'Username and password are required' });
      }
      
      // Find user
      const user = await User.findOne({ where: { username } });
      
      if (!user) {
        return res.status(401).json({ message: 'Invalid credentials' });
      }
      
      // Verify password
      const isPasswordValid = await bcrypt.compare(password, user.password_hash);
      
      if (!isPasswordValid) {
        return res.status(401).json({ message: 'Invalid credentials' });
      }
      
      // Generate JWT token
      const token = jwt.sign(
        { id: user.id, role: user.role },
        process.env.JWT_SECRET || 'your-secret-key',
        { expiresIn: '24h' }
      );
      
      // Return user data and token
      res.json({
        id: user.id,
        username: user.username,
        email: user.email,
        role: user.role,
        token
      });
    } catch (error) {
      console.error('Login error:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred during login.'
      });
    }
  }

  /**
   * Refresh token
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async refresh(req, res) {
    try {
      // User should already be authenticated at this point via middleware
      if (!req.user) {
        return res.status(401).json({ message: 'User not authenticated' });
      }
      
      // Generate a new token
      const token = jwt.sign(
        { id: req.user.id, role: req.user.role },
        process.env.JWT_SECRET || 'your-secret-key',
        { expiresIn: '24h' }
      );
      
      res.json({ token });
    } catch (error) {
      console.error('Token refresh error:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred during token refresh.'
      });
    }
  }
}

module.exports = new AuthController();