import org.jetbrains.kotlin.gradle.tasks.KotlinCompile
import org.gradle.api.tasks.JavaExec

/**
 * Extension to configure SemanticDB settings for source code indexing and analysis.
 */
abstract class SemanticDBExtension {
    var useAnnotationProcessors: Boolean = false
    var enableCrossRepoNavigation: Boolean = true
    var enableScipIndexing: Boolean = true
}

// Create the extension to allow configuration
val semanticDB = extensions.create<SemanticDBExtension>("semanticDB")

// Configure everything after project evaluation when all configurations are available
afterEvaluate {
    val semanticDBVersion = "0.10.3"
    val semanticDbKotlinVersion = "0.4.0"

    // Add required dependencies based on configuration
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

    // Use lazy property to get the semanticdb target directory
    val semanticdbTargetRootProperty = rootProject.layout.buildDirectory.dir("semanticdb-targetroot")
    val semanticdbTargetRoot = semanticdbTargetRootProperty.get().asFile

    // Configure Java compilation tasks
    tasks.withType<JavaCompile>().configureEach {
        val sourceRoot = rootProject.projectDir
        options.compilerArgs.addAll(listOf(
            "-Xplugin:semanticdb",
            "-sourceroot:$sourceRoot",
            "-targetroot:$semanticdbTargetRoot"
        ))
        logger.lifecycle("Configured JavaCompile task '${this.name}' with SemanticDB compiler options (targetroot: $semanticdbTargetRoot)")
    }

    // Configure Kotlin compilation tasks
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

    // Register directory creation task
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

    if (semanticDB.enableCrossRepoNavigation) {
        // Gather project module information during configuration
        val projectModulesMap = mutableMapOf<String, String>()
        rootProject.subprojects.forEach { subproject ->
            projectModulesMap[subproject.projectDir.absolutePath] = subproject.name
        }

        val dependenciesTask = tasks.register<com.sourcegraph.gradle.DependenciesTask>("generateDependenciesTxt") {
            description = "Generates dependencies.txt file for cross-repository navigation"
            group = "sourcegraph"

            dependsOn("createSemanticDBDirectories")

            // Feed the classpath during configuration
            classpath.from(configurations.named("compileClasspath"))

            // Set project coordinates
            projectGroup.set(project.group.toString())
            projectModulesMap.forEach { (path, name) ->
                projectModules.put(path, name)
            }

            outputFile.set(layout.buildDirectory.file("semanticdb-targetroot/dependencies.txt"))
        }

        // Task dependencies remain the same
        tasks.withType<JavaCompile>().configureEach {
            dependsOn(dependenciesTask)
        }

        tasks.withType<KotlinCompile>().configureEach {
            dependsOn(dependenciesTask)
        }
    }




    // Register SCIP indexing tasks
    if (semanticDB.enableScipIndexing) {
        // Make compile tasks depend on directory creation
        tasks.withType<JavaCompile>().configureEach {
            dependsOn("createSemanticDBDirectories")
        }

        tasks.withType<KotlinCompile>().configureEach {
            dependsOn("createSemanticDBDirectories")
        }

        // Register SCIP index generation task
        tasks.register<JavaExec>("generateScipIndex") {
            description = "Generates SCIP index from SemanticDB files"
            group = "sourcegraph"

            dependsOn("createSemanticDBDirectories")
            if (semanticDB.enableCrossRepoNavigation) {
                dependsOn("generateDependenciesTxt")
            }
            dependsOn(tasks.withType<JavaCompile>())
            dependsOn(tasks.withType<KotlinCompile>())

            val scipOutputDir = layout.buildDirectory.dir("scip").get().asFile
            val scipIndexFile = file("${scipOutputDir.absolutePath}/index.scip")

            classpath = configurations.findByName("runtimeClasspath") ?: configurations.findByName("implementation")!!
            mainClass.set("com.sourcegraph.scip_java.ScipJava")

            args = listOf(
                "index-semanticdb",
                semanticdbTargetRoot.absolutePath,
                "--output",
                scipIndexFile.absolutePath
            )

            inputs.dir(semanticdbTargetRoot)
            outputs.file(scipIndexFile)

            doFirst {
                scipOutputDir.mkdirs()
                logger.lifecycle("Looking for SemanticDB files in: $semanticdbTargetRoot")
            }
        }

        // Register the main index generation task that depends on all other tasks
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
