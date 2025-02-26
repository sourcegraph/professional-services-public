plugins {
    id("buildsrc.convention.kotlin-jvm")
    id("org.openapi.generator") version "7.10.0"
    application
}

dependencies {
    // Core implementation dependencies
    implementation(project(":utils"))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:${property("kotlinx_coroutines_version")}")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-jdk8:${property("kotlinx_coroutines_version")}")

    // HTTP client dependencies
    implementation("com.squareup.okhttp3:okhttp:${property("okhttp_version")}")
    implementation("com.squareup.okhttp3:logging-interceptor:${property("okhttp_version")}")

    // JSON serialization dependencies
    implementation("com.squareup.moshi:moshi:${property("moshi_version")}")
    implementation("com.squareup.moshi:moshi-kotlin:${property("moshi_version")}")

    // Ktor dependencies
    implementation("io.ktor:ktor-server-core:1.6.8")
    implementation("io.ktor:ktor-server-netty:1.6.8")
    implementation("io.ktor:ktor-jackson:1.6.8")

    // Add Logback as the SLF4J implementation
    implementation("ch.qos.logback:logback-classic:1.2.11")

    // Test dependencies
    testImplementation("io.kotlintest:kotlintest-runner-junit5:3.4.2")
    testImplementation("io.kotlintest:kotlintest-assertions:3.4.2")


}

application {
    mainClass = "com.sourcegraph.demo.app.AppKt"
}
tasks.openApiGenerate {
    generatorName.set("kotlin")
    inputSpec.set("$projectDir/src/main/resources/openapi.json")
    outputDir.set("$projectDir")
    packageName.set("com.sourcegraph.demo.openapi.generated")
    apiPackage.set("com.sourcegraph.demo.openapi.generated.api")
    modelPackage.set("com.sourcegraph.demo.openapi.generated.model")
    configOptions.set(mapOf(
        "dateLibrary" to "java8",
        "enumPropertyNaming" to "UPPERCASE",
        "serializationLibrary" to "moshi",
        "serializationLibrary" to "moshi",
        "useCoroutines" to "true",
        "omitGradleWrapper" to "true"
    ))
}

// Make sure OpenAPI classes are generated before compilation
tasks.compileKotlin {
    dependsOn(tasks.openApiGenerate)
}

tasks.processResources {
    dependsOn(tasks.openApiGenerate)
}

tasks.clean {
    delete(layout.projectDirectory.dir("docs"))
    delete(layout.projectDirectory.dir(".openapi-generator"))
    delete(layout.projectDirectory.dir("src/main/kotlin/com/sourcegraph/demo/openapi/generated"))
    delete(layout.projectDirectory.dir("src/test/kotlin/com/sourcegraph/demo/openapi/generated"))
}