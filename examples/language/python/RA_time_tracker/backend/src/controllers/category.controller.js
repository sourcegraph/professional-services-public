const categoryService = require('../services/category.service');

/**
 * Category Controller - Handles HTTP requests for category management
 */
class CategoryController {
  /**
   * Get all categories
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getAllCategories(req, res) {
    try {
      const categories = await categoryService.getAllCategories();
      res.json(categories);
    } catch (error) {
      console.error('Error getting categories:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving categories.'
      });
    }
  }

  /**
   * Get a category by ID
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async getCategoryById(req, res) {
    try {
      const id = req.params.id;
      const category = await categoryService.getCategoryById(id);
      
      if (!category) {
        return res.status(404).json({ message: `Category with id ${id} not found` });
      }
      
      res.json(category);
    } catch (error) {
      console.error('Error getting category:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while retrieving the category.'
      });
    }
  }

  /**
   * Create a new category
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async createCategory(req, res) {
    try {
      const { name, description } = req.body;
      
      if (!name) {
        return res.status(400).json({ message: 'Category name is required' });
      }
      
      const newCategory = await categoryService.createCategory(name, description);
      res.status(201).json(newCategory);
    } catch (error) {
      console.error('Error creating category:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while creating the category.'
      });
    }
  }

  /**
   * Update a category
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async updateCategory(req, res) {
    try {
      const id = req.params.id;
      const { name, description, active_status } = req.body;
      
      const updatedCategory = await categoryService.updateCategory(id, { name, description, active_status });
      res.json(updatedCategory);
    } catch (error) {
      console.error('Error updating category:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while updating the category.'
      });
    }
  }

  /**
   * Delete a category
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async deleteCategory(req, res) {
    try {
      const id = req.params.id;
      
      await categoryService.deleteCategory(id);
      res.json({ message: 'Category was deleted successfully!' });
    } catch (error) {
      console.error('Error deleting category:', error);
      res.status(500).json({
        message: error.message || 'Some error occurred while deleting the category.'
      });
    }
  }
}

module.exports = new CategoryController();