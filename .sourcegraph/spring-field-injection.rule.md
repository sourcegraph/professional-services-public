---
title: Use constructor injection instead of field injection
description: Identify when the Autowired annotation is used on a field without a corresponding constructor parameter and suggest using constructor injection instead.
tags: ["dependency-injection", "spring", "java"]
---

Incorrect usage:
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

Correct usage:
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