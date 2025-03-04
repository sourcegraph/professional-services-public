import gradle.kotlin.dsl.accessors._285dcef16d8875fee0ec91e18e07daf9.runtimeClasspath
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

/**
 * Extension to configure SemanticDB settings for source code indexing and analysis.
 *
 * Properties:
 * - useAnnotationProcessors: Whether to use annotation processors for SemanticDB generation
 * - useJavacClassesDirectoryAsTargetRoot: Whether to use the javac classes directory as target root
 * - enableCrossRepoNavigation: Enable cross-repository navigation support
 * - enableScipIndexing: Enable SCIP (Source Code Intelligence Protocol) indexing
 */
abstract class SemanticDBExtension {
    var useAnnotationProcessors: Boolean = false

    var enableCrossRepoNavigation: Boolean = true

    var enableScipIndexing: Boolean = true
}

val semanticDB = extensions.create<SemanticDBExtension>("semanticDB")

/**
 * Configures SemanticDB settings and dependencies after the project has been evaluated.
 * This ensures all project configurations are available before applying SemanticDB-specific setup.
 */
afterEvaluate {
    val semanticDBVersion = "0.10.3"
    val semanticDbKotlinVersion = "0.4.0"

    if (semanticDB.useAnnotationProcessors) {
        dependencies.add("annotationProcessor", "com.sourcegraph:semanticdb-javac:$semanticDBVersion")
    } else {
        dependencies.add("compileOnly", "com.sourcegraph:semanticdb-javac:$semanticDBVersion")
    }

    logger.lifecycle("Added SemanticDB Javac dependency (version $semanticDBVersion) as ${if (semanticDB.useAnnotationProcessors) "annotationProcessor" else "compileOnly"}")

    if (semanticDB.enableScipIndexing) {
        dependencies.add("implementation", "com.sourcegraph:scip-java_2.13:$semanticDBVersion")
        logger.lifecycle("Added SCIP Java dependency (version $semanticDBVersion)")
    }

    val semanticdbTargetRoot = rootProject.layout.buildDirectory.dir("semanticdb-targetroot").get().asFile

    /**
     * Configures JavaCompile tasks with SemanticDB compiler options for source code indexing.
     * Sets up the source root directory and target root directory for SemanticDB output.
     * The target root can either be a dedicated semanticdb directory or the javac classes directory,
     * depending on the SemanticDB extension configuration.
     */
    tasks.withType<JavaCompile>().configureEach {
        val sourceRoot = rootProject.projectDir

        options.compilerArgs.addAll(listOf(
            "-Xplugin:semanticdb",
            "-sourceroot:$sourceRoot",
            "-targetroot:$semanticdbTargetRoot"
        ))

        logger.lifecycle("Configured JavaCompile task '${this.name}' with SemanticDB compiler options (targetroot: $semanticdbTargetRoot)")
    }


    /**
     * Configures KotlinCompile tasks with SemanticDB compiler options for source code indexing.
     * Sets up the source root directory and target root directory for SemanticDB output by adding
     * the SemanticDB Kotlin compiler plugin and its required arguments to the Kotlin compiler options.
     */
    tasks.withType<KotlinCompile>().configureEach {
        val semanticDBJar = project.configurations.detachedConfiguration(
            project.dependencies.create("com.sourcegraph:semanticdb-kotlinc:$semanticDbKotlinVersion")
        ).singleFile

        kotlinOptions.freeCompilerArgs += listOf(
            "-Xplugin=${semanticDBJar}",
            "-P", "plugin:semanticdb-kotlinc:sourceroot=${rootProject.projectDir}",
            "-P", "plugin:semanticdb-kotlinc:targetroot=$semanticdbTargetRoot"
        )

        logger.lifecycle("Configured KotlinCompile task '${this.name}' with SemanticDB compiler options (targetroot: $semanticdbTargetRoot)")
    }

    /**
     * Configures cross-repository navigation support for SemanticDB indexing.
     * Creates a task to generate dependencies.txt file containing resolved compile classpath artifacts,
     * and configures Java and Kotlin compile tasks to depend on this generation.
     * The dependencies file is used by SemanticDB to enable precise code navigation across different repositories.
     */
    if (semanticDB.enableCrossRepoNavigation) {
        tasks.register("generateDependenciesTxt") {
            description = "Generates dependencies.txt file for SemanticDB cross-repository navigation"
            group = "sourcegraph"

            dependsOn("createSemanticDBDirectories")

            // Use layout API for file references
            val targetRootDir = layout.buildDirectory.dir("semanticdb-targetroot")
            val dependenciesFile = targetRootDir.map { it.file("dependencies.txt") }

            // Declare inputs and outputs properly using providers
            inputs.files(configurations.named("compileClasspath"))
            outputs.file(dependenciesFile)

            doLast {
                // Get the file only in the execution phase
                val outputFile = dependenciesFile.get().asFile
                logger.lifecycle("Generating dependencies.txt at $outputFile")

                // Create parent directories
                outputFile.parentFile.mkdirs()

                // Get resolved artifacts through Provider API
                val resolvedArtifacts = configurations.named("compileClasspath").get()
                    .resolvedConfiguration.resolvedArtifacts

                // Write dependencies file
                outputFile.bufferedWriter().use { writer ->
                    resolvedArtifacts.forEach { artifact ->
                        val id = artifact.moduleVersion.id
                        writer.write("${id.group}\t${id.name}\t${id.version}\t${artifact.file.absolutePath}")
                        writer.newLine()
                    }
                }

                logger.lifecycle("Successfully generated dependencies.txt with ${resolvedArtifacts.size} entries")
            }
        }

        /**
         * Configures Java compilation tasks to depend on dependencies.txt generation.
         * This ensures cross-repository navigation data is available before compilation.
         */
        tasks.withType<JavaCompile>().configureEach {
            dependsOn("generateDependenciesTxt")
        }

        /**
         * Configures Kotlin compilation tasks to depend on dependencies.txt generation.
         * This ensures cross-repository navigation data is available before compilation.
         */
        tasks.withType<KotlinCompile>().configureEach {
            dependsOn("generateDependenciesTxt")
        }

        logger.lifecycle("Enabled cross-repository navigation support")
    }

    /**
     * Configures SCIP indexing tasks for generating index files from SemanticDB.
     * Registers two tasks:
     * - generateScipIndex: Converts SemanticDB files to SCIP format
     * - generateSourcegraphIndex: Runs the complete indexing pipeline including compilation
     *
     * The tasks handle both Java and Kotlin compilation, and optionally include
     * cross-repository navigation data when enabled.
     */
    if (semanticDB.enableScipIndexing) {
        // Create a directory creation task that runs before everything else
        tasks.register("createSemanticDBDirectories") {
            description = "Creates directories needed for SemanticDB"
            group = "sourcegraph"

            val scipOutputDir = layout.buildDirectory.dir("scip").get().asFile

            outputs.dir(semanticdbTargetRoot)
            outputs.dir(scipOutputDir)

            doLast {
                semanticdbTargetRoot.mkdirs()
                scipOutputDir.mkdirs()
                logger.lifecycle("Created SemanticDB directories: $semanticdbTargetRoot and $scipOutputDir")
            }
        }

        // Make all the relevant tasks depend on directory creation
        tasks.withType<JavaCompile>().configureEach {
            dependsOn("createSemanticDBDirectories")
        }

        tasks.withType<KotlinCompile>().configureEach {
            dependsOn("createSemanticDBDirectories")
        }

        tasks.register<JavaExec>("generateScipIndex") {
            description = "Generates SCIP index from SemanticDB files"
            group = "sourcegraph"

            // Explicitly declare ALL dependencies
            dependsOn("createSemanticDBDirectories")
            dependsOn("generateDependenciesTxt")
            dependsOn(tasks.withType<JavaCompile>())
            dependsOn(tasks.withType<KotlinCompile>())

            val scipOutputDir = layout.buildDirectory.dir("scip").get().asFile
            val scipIndexFile = file("${scipOutputDir.absolutePath}/index.scip")

            classpath = configurations.runtimeClasspath.get()
            mainClass.set("com.sourcegraph.scip_java.ScipJava")

            args = listOf(
                "index-semanticdb",
                semanticdbTargetRoot.absolutePath,
                "--output",
                scipIndexFile.absolutePath
            )

            doFirst {
                scipOutputDir.mkdirs()
                logger.lifecycle("Looking for SemanticDB files in: $semanticdbTargetRoot")
            }

            // Properly declare inputs that come from other tasks
            inputs.dir(semanticdbTargetRoot)
            outputs.file(scipIndexFile)
        }


        tasks.register("generateSourcegraphIndex") {
            description = "Runs the full Sourcegraph indexing pipeline"
            group = "sourcegraph"

            dependsOn(tasks.withType<JavaCompile>())
            dependsOn(tasks.withType<KotlinCompile>())

            if (semanticDB.enableCrossRepoNavigation) {
                dependsOn("generateDependenciesTxt")
            }

            dependsOn("generateScipIndex")
        }

        logger.lifecycle("Enabled SCIP indexing support")
    }
}