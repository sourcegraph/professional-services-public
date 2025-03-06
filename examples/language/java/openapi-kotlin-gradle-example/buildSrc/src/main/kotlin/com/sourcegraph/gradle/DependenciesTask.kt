import org.gradle.api.DefaultTask
import org.gradle.api.tasks.Input
import org.gradle.api.tasks.InputFiles
import org.gradle.api.tasks.OutputFile
import org.gradle.api.tasks.TaskAction
import org.gradle.api.file.ConfigurableFileCollection
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.provider.MapProperty
import org.gradle.api.provider.Property

// Custom task for generating dependencies.txt file
abstract class DependenciesTask : DefaultTask() {
    @get:InputFiles
    abstract val classpath: ConfigurableFileCollection

    @get:Input
    abstract val projectGroup: Property<String>

    @get:Input
    abstract val projectModules: MapProperty<String, String>

    @get:OutputFile
    abstract val outputFile: RegularFileProperty

    @TaskAction
    fun generateDependenciesTxt() {
        val classpathEntries = classpath.files.map { it.absolutePath }
        val dependenciesContent = buildString {
            appendLine("# Project dependencies for cross-repository navigation")
            appendLine("groupId: ${projectGroup.get()}")
            appendLine()
            appendLine("# Project modules")
            projectModules.get().forEach { (path, name) ->
                appendLine("module: $path -> $name")
            }
            appendLine()
            appendLine("# Classpath dependencies")
            classpathEntries.forEach { entry ->
                appendLine("classpath: $entry")
            }
        }

        val outputFileObj = outputFile.get().asFile
        outputFileObj.parentFile.mkdirs()
        outputFileObj.writeText(dependenciesContent)

        logger.lifecycle("Generated dependencies.txt at ${outputFileObj.absolutePath}")
    }
}

