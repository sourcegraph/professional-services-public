const resourceService = require('../services/resource.service');

/**
 * Resource Controller - Handles HTTP requests for resource management
 */
class ResourceController {
  /**
   * Get all resources
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getAllResources(req, res) {
    try {
      const resources = await resourceService.getAllResources();
      res.json(resources);
    } catch (error) {
      console.error('Error getting resources:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving resources.'
      });
    }
  }

  /**
   * Get a resource by ID
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getResourceById(req, res) {
    try {
      const id = req.params.id;
      const resource = await resourceService.getResourceById(id);
      
      if (!resource) {
        return res.status(404).json({ message: `Resource with id ${id} not found` });
      }
      
      res.json(resource);
    } catch (error) {
      console.error('Error getting resource:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving the resource.'
      });
    }
  }

  /**
   * Create a new resource
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async createResource(req, res) {
    try {
      const { name } = req.body;
      
      if (!name) {
        return res.status(400).json({ message: 'Resource name is required' });
      }
      
      const newResource = await resourceService.createResource(name);
      res.status(201).json(newResource);
    } catch (error) {
      console.error('Error creating resource:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while creating the resource.'
      });
    }
  }

  /**
   * Update a resource
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async updateResource(req, res) {
    try {
      const id = req.params.id;
      const { name, active_status } = req.body;
      
      const updatedResource = await resourceService.updateResource(id, { name, active_status });
      res.json(updatedResource);
    } catch (error) {
      console.error('Error updating resource:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while updating the resource.'
      });
    }
  }

  /**
   * Delete a resource
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async deleteResource(req, res) {
    try {
      const id = req.params.id;
      
      await resourceService.deleteResource(id);
      res.json({ message: 'Resource was deleted successfully!' });
    } catch (error) {
      console.error('Error deleting resource:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while deleting the resource.'
      });
    }
  }
}

module.exports = new ResourceController();