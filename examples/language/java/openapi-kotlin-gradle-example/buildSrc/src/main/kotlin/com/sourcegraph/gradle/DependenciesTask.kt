package com.sourcegraph.gradle

import org.gradle.api.DefaultTask
import org.gradle.api.file.ConfigurableFileCollection
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.tasks.*
import java.io.File

abstract class DependenciesTask : DefaultTask() {
    @get:InputFiles
    abstract val classpath: ConfigurableFileCollection

    @get:OutputFile
    abstract val outputFile: RegularFileProperty

    @TaskAction
    fun generateDependenciesTxt() {
        val file = outputFile.get().asFile
        file.parentFile.mkdirs()

        file.bufferedWriter().use { writer ->
            // Get the files directly from the ConfigurableFileCollection property
            // No access to project.configurations here
            classpath.files.forEach { depFile ->
                // We don't have the full metadata, so we'll use the filename to extract
                // some basic information or just use placeholder values
                val fileName = depFile.name
                val parts = fileName.split("-")

                // This is a simplistic approach - in real code you might want more robust extraction
                if (parts.size >= 2) {
                    val lastIndex = parts.lastIndex
                    val name = parts.subList(0, lastIndex).joinToString("-")
                    val version = parts[lastIndex].removeSuffix(".jar")

                    writer.write("extracted\t$name\t$version\t${depFile.absolutePath}")
                } else {
                    writer.write("unknown\tunknown\tunknown\t${depFile.absolutePath}")
                }
                writer.newLine()
            }
        }

        logger.lifecycle("Generated dependencies.txt at ${file}")
    }
}
