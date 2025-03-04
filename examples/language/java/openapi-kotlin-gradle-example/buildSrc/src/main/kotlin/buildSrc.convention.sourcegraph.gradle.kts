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

    var useJavacClassesDirectoryAsTargetRoot: Boolean = false

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
    val scipJavaVersion = semanticDBVersion

    if (semanticDB.useAnnotationProcessors) {
        dependencies.add("annotationProcessor", "com.sourcegraph:semanticdb-javac:$semanticDBVersion")
    } else {
        dependencies.add("compileOnly", "com.sourcegraph:semanticdb-javac:$semanticDBVersion")
    }

    logger.lifecycle("Added SemanticDB Javac dependency (version $semanticDBVersion) as ${if (semanticDB.useAnnotationProcessors) "annotationProcessor" else "compileOnly"}")

    if (semanticDB.enableScipIndexing) {
        dependencies.add("implementation", "com.sourcegraph:scip-java_2.13:$scipJavaVersion")
        logger.lifecycle("Added SCIP Java dependency (version $scipJavaVersion)")
    }

    val targetRootDir = if (semanticDB.useJavacClassesDirectoryAsTargetRoot) {
        rootProject.layout.buildDirectory.dir("javac-classes-directory").get().asFile
    } else {
        rootProject.layout.buildDirectory.dir("semanticdb-targetroot").get().asFile
    }


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
            "-sourceroot:$sourceRoot"
        ))


        if (semanticDB.useJavacClassesDirectoryAsTargetRoot) {
            options.compilerArgs.add("-targetroot:javac-classes-directory")
        } else {
            options.compilerArgs.add("-targetroot:$targetRootDir")
        }

        logger.lifecycle("Configured JavaCompile task '${this.name}' with SemanticDB compiler options")
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
            "-P", "plugin:semanticdb-kotlinc:targetroot=${project.layout.buildDirectory.dir("semanticdb-targetroot").get().asFile.absolutePath}"
        )

        logger.lifecycle("Configured KotlinCompile task '${this.name}' with SemanticDB compiler options")
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

        // Use layout API for file references
        val targetrootDir = layout.buildDirectory.dir("semanticdb-targetroot")
        val dependenciesFile = targetrootDir.map { it.file("dependencies.txt") }

        // Declare inputs and outputs properly
        inputs.files(configurations.named("compileClasspath"))
        outputs.file(dependenciesFile)

        doLast {
            // Create parent directories
            dependenciesFile.get().asFile.parentFile.mkdirs()

            // Get resolved artifacts through Provider API instead of direct project access
            val dependencies = configurations.named("compileClasspath").get()
                .resolvedConfiguration.resolvedArtifacts

            // Write dependencies file
            dependenciesFile.get().asFile.bufferedWriter().use { writer ->
                dependencies.forEach { artifact ->
                    val id = artifact.moduleVersion.id
                    writer.write("${id.group}\t${id.name}\t${id.version}\t${artifact.file.absolutePath}")
                    writer.newLine()
                }
            }

            logger.lifecycle("Generated dependencies.txt at ${dependenciesFile.get()}")
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
     * Configures SCIP indexing tasks when enabled.
     * Registers two tasks:
     * - generateScipIndex: Converts SemanticDB files into SCIP index format for code intelligence
     * - generateSourcegraphIndex: Runs the complete indexing pipeline including compilation and SCIP generation
     *
     * The SCIP index is used by Sourcegraph for precise code navigation and analysis.
     * Depends on Java/Kotlin compilation tasks and optionally cross-repository navigation data.
     */
    if (semanticDB.enableScipIndexing) {
        tasks.register<JavaExec>("generateScipIndex") {
            description = "Generates SCIP index from SemanticDB files"
            group = "sourcegraph"

            val targetrootDir = layout.buildDirectory.dir("semanticdb-targetroot")
            val scipOutputDir = layout.buildDirectory.dir("scip")
            val scipIndexFile = scipOutputDir.map { it.file("index.scip") }

            classpath = configurations.named("runtimeClasspath").get()
            mainClass.set("com.sourcegraph.scip_java.ScipJava")

            args = listOf(
                "index-semanticdb",
                targetrootDir.get().asFile.absolutePath,
                "--output",
                scipIndexFile.get().asFile.absolutePath
            )

            doFirst {
                scipOutputDir.get().asFile.mkdirs()
            }

            mustRunAfter(tasks.withType<JavaCompile>())
            mustRunAfter(tasks.withType<KotlinCompile>())

            inputs.dir(targetrootDir)
            outputs.file(scipIndexFile)
        }
        /**
         * Registers the main Sourcegraph indexing task that runs the complete pipeline.
         * This task coordinates all necessary steps for code intelligence:
         * - Java/Kotlin compilation
         * - Cross-repository navigation data generation (if enabled)
         * - SCIP index generation from SemanticDB files
         *
         * The task ensures proper sequencing of dependent tasks to produce a complete
         * code intelligence index for Sourcegraph.
         */
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