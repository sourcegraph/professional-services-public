module.exports = (sequelize, DataTypes) => {
  const Month = sequelize.define('Month', {
    id: {
      type: DataTypes.INTEGER,
      primaryKey: true,
      autoIncrement: true
    },
    year: {
      type: DataTypes.INTEGER,
      allowNull: false
    },
    month: {
      type: DataTypes.INTEGER,
      allowNull: false,
      validate: {
        min: 1,
        max: 12
      }
    },
    created_at: {
      type: DataTypes.DATE,
      defaultValue: DataTypes.NOW
    },
    updated_at: {
      type: DataTypes.DATE,
      defaultValue: DataTypes.NOW
    }
  }, {
    tableName: 'months',
    timestamps: true,
    underscored: true,
    indexes: [
      {
        unique: true,
        fields: ['year', 'month']
      }
    ]
  });

  return Month;
};