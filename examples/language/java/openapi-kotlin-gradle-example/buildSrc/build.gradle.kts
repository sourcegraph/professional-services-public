plugins {
    `kotlin-dsl`
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("org.jetbrains.kotlin:kotlin-gradle-plugin:1.9.22")
    testImplementation("io.kotest:kotest-runner-junit5-jvm:5.9.1")
}