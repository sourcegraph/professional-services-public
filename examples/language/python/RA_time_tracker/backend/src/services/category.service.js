const db = require('../models');
const { Category } = db;

/**
 * Category Service - Handles category management
 */
class CategoryService {
  /**
   * Get all categories
   * @returns {Array} List of categories
   */
  async getAllCategories() {
    return Category.findAll({
      where: { active_status: true },
      order: [['name', 'ASC']]
    });
  }

  /**
   * Get a category by ID
   * @param {number} id - Category ID
   * @returns {Object} Category
   */
  async getCategoryById(id) {
    return Category.findByPk(id);
  }

  /**
   * Create a new category
   * @param {string} name - Category name
   * @param {string} description - Category description
   * @returns {Object} Created category
   */
  async createCategory(name, description) {
    return Category.create({
      name,
      description,
      active_status: true
    });
  }

  /**
   * Update a category
   * @param {number} id - Category ID
   * @param {Object} data - Category data to update
   * @returns {Object} Updated category
   */
  async updateCategory(id, data) {
    const category = await Category.findByPk(id);
    
    if (!category) {
      throw new Error(`Category with id ${id} not found`);
    }
    
    return category.update(data);
  }

  /**
   * Delete a category (soft delete)
   * @param {number} id - Category ID
   * @returns {boolean} Success status
   */
  async deleteCategory(id) {
    const category = await Category.findByPk(id);
    
    if (!category) {
      throw new Error(`Category with id ${id} not found`);
    }
    
    await category.update({ active_status: false });
    return true;
  }
}

module.exports = new CategoryService();