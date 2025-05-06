# Spring Boot 2.7 to 3.x Migration Example

This project demonstrates key migration points when upgrading from Spring Boot 2.7 to 3.x.

## Migration Aspects

### 1. Jakarta EE Namespace Changes
Current implementation uses `javax.persistence.*` which will change to `jakarta.persistence.*` in Spring Boot 3.x. This affects:
- Entity annotations (@Entity, @Id, @GeneratedValue)
- Validation annotations
- Servlet components
- JPA related code

### 2. Deprecated API Updates
Several APIs are deprecated in 2.7 and removed in 3.x:
- WebSecurityConfigurerAdapter replaced with component-based security
- CrudRepository method signatures refined
- Spring MVC configuration patterns
- ResponseEntity builder patterns
- WebMvcConfigurer implementations

### 3. Database Configuration Changes
Database handling updates include:
- H2 database version compatibility
- Hibernate 6.x as default ORM
- Updated dialect naming conventions
- HikariCP connection pool defaults
- Enhanced transaction management

### 4. Security Defaults Modifications
Security enhancements in 3.x:
- Stricter CSRF protection
- Enhanced security headers
- Updated password encoding defaults
- Modified session management
- Stronger CORS configuration requirements

## Project Structure

The project includes:
- REST endpoint (/api/products)
- JPA entity (Product)
- H2 database configuration
- Service layer implementation
- Controller layer implementation

## Running the Application

1. Start the application:

```bash
./gradlew bootRun
```

2. Access the endpoints:
* Products API: http://localhost:8080/api/products
* H2 Console: http://localhost:8080/h2-console