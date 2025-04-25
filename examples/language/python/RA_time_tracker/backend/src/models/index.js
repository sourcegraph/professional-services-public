const { Sequelize } = require('sequelize');
const config = require('../config/database');

const sequelize = new Sequelize(
  config.database,
  config.username,
  config.password,
  {
    host: config.host,
    dialect: 'postgres',
    logging: false,
  }
);

const db = {};

db.Sequelize = Sequelize;
db.sequelize = sequelize;

// Import models
db.User = require('./user.model')(sequelize, Sequelize);
db.Resource = require('./resource.model')(sequelize, Sequelize);
db.Category = require('./category.model')(sequelize, Sequelize);
db.Month = require('./month.model')(sequelize, Sequelize);
db.Week = require('./week.model')(sequelize, Sequelize);
db.TimeEntry = require('./timeEntry.model')(sequelize, Sequelize);

// Set up associations
db.Month.hasMany(db.Week, { as: 'weeks', foreignKey: 'month_id' });
db.Week.belongsTo(db.Month, { foreignKey: 'month_id' });

db.Week.hasMany(db.TimeEntry, { as: 'timeEntries', foreignKey: 'week_id' });
db.TimeEntry.belongsTo(db.Week, { foreignKey: 'week_id' });

db.Resource.hasMany(db.TimeEntry, { as: 'timeEntries', foreignKey: 'resource_id' });
db.TimeEntry.belongsTo(db.Resource, { foreignKey: 'resource_id' });

db.Category.hasMany(db.TimeEntry, { as: 'timeEntries', foreignKey: 'category_id' });
db.TimeEntry.belongsTo(db.Category, { foreignKey: 'category_id' });

module.exports = db;