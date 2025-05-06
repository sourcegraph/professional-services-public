const jwt = require('jsonwebtoken');
const { User } = require('../models');

/**
 * Authentication Middleware
 */
class AuthMiddleware {
  /**
   * Verify JWT token in Authorization header
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   * @param {Function} next - Express next function
   */
  async verifyToken(req, res, next) {
    try {
      const authHeader = req.headers.authorization;
      
      if (!authHeader) {
        return res.status(401).json({ message: 'No token provided' });
      }
      
      const token = authHeader.split(' ')[1]; // Format: "Bearer TOKEN"
      
      if (!token) {
        return res.status(401).json({ message: 'No token provided' });
      }
      
      // Verify token
      const decoded = jwt.verify(token, process.env.JWT_SECRET || 'your-secret-key');
      
      // Check if user exists
      const user = await User.findByPk(decoded.id);
      
      if (!user) {
        return res.status(401).json({ message: 'User not found' });
      }
      
      // Attach user to request
      req.user = {
        id: user.id,
        username: user.username,
        email: user.email,
        role: user.role
      };
      
      next();
    } catch (error) {
      if (error.name === 'TokenExpiredError') {
        return res.status(401).json({ message: 'Token expired' });
      }
      
      return res.status(401).json({ message: 'Unauthorized' });
    }
  }

  /**
   * Check if user has admin role
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   * @param {Function} next - Express next function
   */
  isAdmin(req, res, next) {
    if (req.user && req.user.role === 'admin') {
      next();
    } else {
      res.status(403).json({ message: 'Require Admin Role!' });
    }
  }

  /**
   * Check if user has manager role or higher
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   * @param {Function} next - Express next function
   */
  isManagerOrAdmin(req, res, next) {
    if (req.user && (req.user.role === 'manager' || req.user.role === 'admin')) {
      next();
    } else {
      res.status(403).json({ message: 'Require Manager Role!' });
    }
  }
}

module.exports = new AuthMiddleware();