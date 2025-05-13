# Sourcegraph Professional Services Coding Guidelines

## Build/Test Commands

### TypeScript/JavaScript
- Build: `npm run build` or `yarn build`
- Test: `npm test` or `yarn test`
- Single test: `npx jest <file-path>` or `jest -t "test name"`

### Java/Kotlin
- Build: `./gradlew build`
- Test: `./gradlew test`
- Single test: `./gradlew test --tests "<TestClassName>"`

## Code Style Guidelines

### TypeScript
- Use strict type checking (`strict: true` in tsconfig.json)
- Prefer ES modules and modern syntax
- Use interfaces for defining types
- Follow error handling conventions using HTTP status codes for APIs

### Spring Framework
- Use constructor injection instead of field injection with @Autowired
- Make dependencies final to ensure immutability
- Write testable components with explicit dependencies

### General
- Follow existing patterns in the codebase
- All public code must meet PUBLIC data classification requirements
- Use Pet Store API example for API implementations: https://petstore3.swagger.io/