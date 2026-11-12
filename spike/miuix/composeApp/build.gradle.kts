// Desktop-only first pass. Android / wasmJs targets are added in T3 once the
// desktop target proves the dependency graph resolves and Miuix renders.
import org.jetbrains.compose.desktop.application.dsl.TargetFormat

plugins {
    kotlin("multiplatform")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.compose")
}

kotlin {
    jvm("desktop")

    sourceSets {
        val desktopMain by getting

        commonMain.dependencies {
            implementation(compose.runtime)
            implementation(compose.foundation)
            implementation(compose.ui)
        }

        desktopMain.dependencies {
            implementation(compose.desktop.currentOs)
        }

        commonMain.dependencies {
            // Pinned: 0.9.4 is the newest published stable (2026-09-20) and is
            // compiled against Kotlin 2.4.20 / CMP 1.12.0, which is why the
            // root build pins those exact versions.
            implementation("top.yukonga.miuix.kmp:miuix-ui:0.9.4")
        }
    }
}

compose.desktop {
    application {
        mainClass = "vade.spike.miuix.MainKt"
        nativeDistributions {
            targetFormats(TargetFormat.Deb)
            packageName = "vade-miuix-spike"
            packageVersion = "0.0.1"
        }
    }
}

// Headless render: same composables, no X server, one PNG per component.
val desktopMainCompilation = kotlin.targets.getByName("desktop").compilations.getByName("main")

// Lets the Xvfb pass launch the app without paying Gradle configuration per shot.
tasks.register("printRuntimeClasspath") {
    doLast {
        println(
            files(desktopMainCompilation.output.allOutputs, desktopMainCompilation.runtimeDependencyFiles).asPath,
        )
    }
}

tasks.register<JavaExec>("screenshot") {
    group = "verification"
    description = "Render every showcase section to shots/*.png without a display"
    dependsOn("desktopMainClasses")
    mainClass.set("vade.spike.miuix.ShotKt")
    classpath = files(desktopMainCompilation.output.allOutputs, desktopMainCompilation.runtimeDependencyFiles)
    args = listOf(rootProject.layout.projectDirectory.dir("shots").asFile.absolutePath)
    systemProperty("java.awt.headless", "true")
}
