import org.springframework.boot.gradle.tasks.run.BootRun

plugins {
    application
    id("org.openapi.generator") version "7.10.0"
    id("org.springframework.boot") version "3.4.3"
    id("com.github.node-gradle.node") version "3.5.1"
}

apply(plugin = "io.spring.dependency-management")

repositories {
    // Use Maven Central for resolving dependencies.
    mavenCentral()
}

dependencies {
    // Spring Boot dependencies
    implementation(libs.spring.boot.starter.web)
    implementation(libs.spring.boot.starter.webflux)
    implementation(libs.spring.boot.starter.security)
    testImplementation(libs.spring.boot.starter.test)

    // Jackson dependencies - required for OpenAPI client
    implementation(libs.jackson.databind)
    implementation(libs.jackson.datatype.jsr310)
    implementation(libs.jackson.annotations)
    implementation(libs.jackson.core)

    // Jakarta dependencies
    implementation(libs.jakarta.annotation.api)

    // OpenAPI tools dependencies
    implementation(libs.openapitools.jackson.nullable)

    // Use JUnit Jupiter for testing.
    testImplementation(libs.junit.jupiter)
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")

    // This dependency is used by the application.
    implementation(libs.guava)
}

// Apply a specific Java toolchain to ease working on different environments.
java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(17)
    }
}

node {
    version.set("18.12.1")
    download.set(true)
    workDir.set(file("${project.buildDir}/nodejs"))
    npmWorkDir.set(file("${project.buildDir}/npm"))
}

application {
    // Define the main class for the application.
    mainClass = "com.sourcegraph.petstore.App"
}

tasks.named<Test>("test") {
    // Use JUnit Platform for unit tests.
    useJUnitPlatform()
}

// OpenAPI generator configuration
tasks.openApiGenerate {
    generatorName.set("java")
    inputSpec.set("$projectDir/src/main/resources/pet-store.json")
    outputDir.set("$projectDir/petstore-openapi-client")
    packageName.set("com.sourcegraph.petstore.openapi.generated")
    apiPackage.set("com.sourcegraph.petstore.openapi.generated.api")
    modelPackage.set("com.sourcegraph.petstore.openapi.generated.model")
    configOptions.set(mapOf(
        "artifactId" to "petstore-openapi-client",
        "documentationProvider" to "none",
        "dateLibrary" to "java8",
        "serializationLibrary" to "jackson",
        "library" to "restclient",
        "omitGradleWrapper" to "true"
    ))
}

tasks.clean {
    delete(layout.projectDirectory.dir("petstore-openapi-client"))
}

sourceSets {
    create("openapi") {
        java {
            srcDir("${tasks.openApiGenerate.get().outputDir.get()}/src/main/java")
        }
    }

    main {
        compileClasspath += sourceSets["openapi"].output
        runtimeClasspath += sourceSets["openapi"].output
    }
}

// Configure openapi source set to have the same dependencies as main
configurations {
    val openapiImplementation by getting {
        extendsFrom(configurations.implementation.get())
    }
}

// Add explicit dependency from compileOpenapiJava to openApiGenerate
tasks.named("compileOpenapiJava") {
    dependsOn(tasks.openApiGenerate)
}

// Make main Java compilation depend on the openapi compilation
tasks.named("compileJava") {
    dependsOn(tasks.named("compileOpenapiJava"))
}


tasks.named("processResources") {
    dependsOn("buildReact")
    doLast {
        copy {
            from("${project.projectDir}/src/main/webapp/frontend/build")
            into("${project.buildDir}/resources/main/static")
        }
    }
}

tasks.register<com.github.gradle.node.npm.task.NpmTask>("buildReact") {
    dependsOn(tasks.npmInstall)
    workingDir.set(file("${project.projectDir}/src/main/webapp/frontend"))
    args.set(listOf("run", "build"))
}

