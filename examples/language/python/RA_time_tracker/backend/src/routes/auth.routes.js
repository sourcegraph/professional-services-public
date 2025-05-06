const express = require('express');
const authController = require('../controllers/auth.controller');
const authMiddleware = require('../middleware/auth.middleware');

const router = express.Router();

// POST /api/auth/register - Register a new user
router.post('/register', authController.register);

// POST /api/auth/login - Login a user
router.post('/login', authController.login);

// POST /api/auth/refresh - Refresh authentication token
router.post('/refresh', authMiddleware.verifyToken, authController.refresh);

module.exports = router;