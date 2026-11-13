// Three targets: androidTarget (APK size), jvm("desktop") (headless render),
// wasmJs (the point of the spike: does Miuix publish a wasm variant, and what
// does it cost in bytes).
import org.jetbrains.compose.desktop.application.dsl.TargetFormat
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("multiplatform")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.compose")
    id("com.android.application")
}

kotlin {
    androidTarget {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }

    jvm("desktop")

    @OptIn(org.jetbrains.kotlin.gradle.ExperimentalWasmDsl::class)
    wasmJs {
        // `moduleName` was renamed to `outputModuleName` in KGP 1.9; 2.4.20 has
        // no `moduleName` accessor at all (build script fails to compile).
        outputModuleName = "composeApp"
        browser {
            commonWebpackConfig {
                outputFileName = "composeApp.js"
            }
        }
        binaries.executable()
    }

    sourceSets {
        val desktopMain by getting
        val wasmJsMain by getting

        commonMain.dependencies {
            implementation(compose.runtime)
            implementation(compose.foundation)
            implementation(compose.ui)
            implementation(compose.material3)
            // Pinned: 0.9.4 is the newest published stable (2026-09-20) and is
            // compiled against Kotlin 2.4.20 / CMP 1.12.0, which is why the
            // root build pins those exact versions.
            implementation("top.yukonga.miuix.kmp:miuix-ui:0.9.4")
        }

        desktopMain.dependencies {
            implementation(compose.desktop.currentOs)
        }

        androidMain.dependencies {
            implementation("androidx.activity:activity-compose:1.9.3")
        }

        wasmJsMain.dependencies {
            // Nothing extra: ComposeViewport + Miuix are the whole surface.
        }
    }
}

android {
    namespace = "vade.spike.miuix"
    compileSdk = 36

    defaultConfig {
        applicationId = "vade.spike.miuix"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
}

compose.desktop {
    application {
        mainClass = "vade.spike.miuix.MainKt"
        nativeDistributions {
            // Was TargetFormat.Deb on the Linux host. jpackage on Windows can
            // only emit Msi/Exe; the spike never actually packages (it renders
            // headless), this only has to configure cleanly.
            targetFormats(TargetFormat.Msi)
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
