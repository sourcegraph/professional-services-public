package com.sourcegraph.gradle

import org.gradle.api.DefaultTask
import org.gradle.api.file.ConfigurableFileCollection
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.provider.MapProperty
import org.gradle.api.provider.Property
import org.gradle.api.tasks.*

abstract class DependenciesTask : DefaultTask() {
    @get:InputFiles
    abstract val classpath: ConfigurableFileCollection

    @get:OutputFile
    abstract val outputFile: RegularFileProperty

    // Add properties to store project coordinates
    @get:Input
    abstract val projectGroup: Property<String>

    // Store a map of project module paths to their names
    @get:Input
    abstract val projectModules: MapProperty<String, String>

    @TaskAction
    fun generateDependenciesTxt() {
        val file = outputFile.get().asFile
        file.parentFile.mkdirs()

        val group = projectGroup.get()
        val modules = projectModules.get()

        file.bufferedWriter().use { writer ->
            classpath.files.forEach { depFile ->
                val absolutePath = depFile.absolutePath

                // Check if this is a project module JAR
                val moduleEntry = modules.entries.find { (path, _) ->
                    absolutePath.contains("$path/build/")
                }

                if (moduleEntry != null) {
                    // This is a project module JAR
                    val moduleName = moduleEntry.value
                    // Use project's group and module name as artifact ID
                    writer.write("$group\t$moduleName\t1.0\t$absolutePath")
                } else {
                    // External dependency - extract from filename
                    val fileName = depFile.name
                    val parts = fileName.split("-")

                    if (parts.size >= 2) {
                        val lastIndex = parts.lastIndex
                        val name = parts.subList(0, lastIndex).joinToString("-")
                        val version = parts[lastIndex].removeSuffix(".jar")

                        writer.write("extracted\t$name\t$version\t$absolutePath")
                    } else {
                        writer.write("unknown\tunknown\tunknown\t$absolutePath")
                    }
                }
                writer.newLine()
            }
        }

        logger.lifecycle("Generated dependencies.txt at $file")
    }
}
