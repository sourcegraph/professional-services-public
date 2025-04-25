const express = require('express');
const resourceController = require('../controllers/resource.controller');
const authMiddleware = require('../middleware/auth.middleware');

const router = express.Router();

// Apply authentication middleware to all routes
router.use(authMiddleware.verifyToken);

// GET /api/resources - Get all resources
router.get('/', resourceController.getAllResources);

// GET /api/resources/:id - Get a resource by ID
router.get('/:id', resourceController.getResourceById);

// POST /api/resources - Create a new resource
router.post('/', authMiddleware.isManagerOrAdmin, resourceController.createResource);

// PUT /api/resources/:id - Update a resource
router.put('/:id', authMiddleware.isManagerOrAdmin, resourceController.updateResource);

// DELETE /api/resources/:id - Delete a resource (soft delete)
router.delete('/:id', authMiddleware.isManagerOrAdmin, resourceController.deleteResource);

module.exports = router;