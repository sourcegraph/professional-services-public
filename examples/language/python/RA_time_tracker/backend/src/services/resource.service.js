const db = require('../models');
const { Resource } = db;

/**
 * Resource Service - Handles resource management
 */
class ResourceService {
  /**
   * Get all resources
   * @returns {Array} List of resources
   */
  async getAllResources() {
    return Resource.findAll({
      where: { active_status: true },
      order: [['name', 'ASC']]
    });
  }

  /**
   * Get a resource by ID
   * @param {number} id - Resource ID
   * @returns {Object} Resource
   */
  async getResourceById(id) {
    return Resource.findByPk(id);
  }

  /**
   * Create a new resource
   * @param {string} name - Resource name
   * @returns {Object} Created resource
   */
  async createResource(name) {
    return Resource.create({
      name,
      active_status: true
    });
  }

  /**
   * Update a resource
   * @param {number} id - Resource ID
   * @param {Object} data - Resource data to update
   * @returns {Object} Updated resource
   */
  async updateResource(id, data) {
    const resource = await Resource.findByPk(id);
    
    if (!resource) {
      throw new Error(`Resource with id ${id} not found`);
    }
    
    return resource.update(data);
  }

  /**
   * Delete a resource (soft delete)
   * @param {number} id - Resource ID
   * @returns {boolean} Success status
   */
  async deleteResource(id) {
    const resource = await Resource.findByPk(id);
    
    if (!resource) {
      throw new Error(`Resource with id ${id} not found`);
    }
    
    await resource.update({ active_status: false });
    return true;
  }
}

module.exports = new ResourceService();