const express = require('express');
const categoryController = require('../controllers/category.controller');
const authMiddleware = require('../middleware/auth.middleware');

const router = express.Router();

// Apply authentication middleware to all routes
router.use(authMiddleware.verifyToken);

// GET /api/categories - Get all categories
router.get('/', categoryController.getAllCategories);

// GET /api/categories/:id - Get a category by ID
router.get('/:id', categoryController.getCategoryById);

// POST /api/categories - Create a new category
router.post('/', authMiddleware.isManagerOrAdmin, categoryController.createCategory);

// PUT /api/categories/:id - Update a category
router.put('/:id', authMiddleware.isManagerOrAdmin, categoryController.updateCategory);

// DELETE /api/categories/:id - Delete a category (soft delete)
router.delete('/:id', authMiddleware.isManagerOrAdmin, categoryController.deleteCategory);

module.exports = router;