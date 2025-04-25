const bcrypt = require('bcrypt');

async function verifyPassword() {
  const storedHash = '$2b$10$EmKx9mQQ3mFZE4XHqy.m9eLUY5o8OOVVvMPQS86LqCXYwZjbLh4JS';
  const password = 'admin123';
  
  try {
    const isMatch = await bcrypt.compare(password, storedHash);
    console.log(`Password match result: ${isMatch}`);
    
    // If password doesn't match, generate a new hash
    if (!isMatch) {
      const salt = await bcrypt.genSalt(10);
      const newHash = await bcrypt.hash(password, salt);
      console.log(`New hash for 'admin123': ${newHash}`);
    }
  } catch (error) {
    console.error('Error verifying password:', error);
  }
}

verifyPassword();