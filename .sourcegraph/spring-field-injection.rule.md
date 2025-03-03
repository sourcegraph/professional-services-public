---
title: Use constructor injection instead of field injection
description: Identify when the Autowired annotation is used on a field without a corresponding constructor parameter and suggest using constructor injection instead.
tags: ["dependency-injection", "spring", "java"]
---

## Why Constructor Injection Is Better
1. Immutability: Using final fields guarantees dependencies can't be changed after initialization, promoting safer code.

2. Explicit Dependencies: The constructor clearly shows what dependencies are required for the class to function.

3. Testability: Constructor injection makes unit testing straightforward:

    ```java
    @Test
    public void testCreateUser() {
        UserRepository mockRepository = mock(UserRepository.class);
        EmailService mockEmailService = mock(EmailService.class);
        
        UserService userService = new UserService(mockRepository, mockEmailService);
        
        // Test the service with mocked dependencies
        userService.createUser("John", "john@example.com");
        
        verify(mockRepository).save(any(User.class));
        verify(mockEmailService).sendWelcomeEmail(any(User.class));
    }
    ```

4. Circular Dependency Detection: Constructor injection fails fast when circular dependencies exist, revealing design problems immediately.

5. Mandatory Dependencies: It enforces that all required dependencies are provided at object creation time.

6. Framework Independence: The class can work outside the Spring container, as it doesn't rely on Spring's reflection-based injection.

Constructor injection is now the recommended approach in Spring Boot applications and is aligned with the SOLID principles of software design, particularly the Dependency Inversion Principle.

## Incorrect usage
```java
@Service
public class UserService {
    @Autowired
    private UserRepository userRepository;
    
    @Autowired
    private EmailService emailService;
    
    public User createUser(String name, String email) {
        User user = new User(name, email);
        userRepository.save(user);
        emailService.sendWelcomeEmail(user);
        return user;
    }
}
```

## Correct usage
```java
@Service
public class UserService {
    private final UserRepository userRepository;
    private final EmailService emailService;
    
    public UserService(UserRepository userRepository, EmailService emailService) {
        this.userRepository = userRepository;
        this.emailService = emailService;
    }
    
    public User createUser(String name, String email) {
        User user = new User(name, email);
        userRepository.save(user);
        emailService.sendWelcomeEmail(user);
        return user;
    }
}
```

