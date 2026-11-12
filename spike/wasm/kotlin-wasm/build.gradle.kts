plugins {
    kotlin("multiplatform") version "2.2.21"
}

repositories {
    maven("https://maven.aliyun.com/repository/public")
    mavenCentral()
}

// Kotlin copies the npm spec verbatim into the generated package.json under
// build/wasm/, so a relative "file:" path resolves against the wrong directory
// and becomes a broken symlink. Absolute paths survive the copy.
val protocolPkgDir = layout.projectDirectory.dir("../protocol-wasm/pkg").asFile.absolutePath
val bridgePkgDir = layout.projectDirectory.dir("npm/vade-bridge").asFile.absolutePath

kotlin {
    wasmJs {
        nodejs()
        binaries.executable()
    }

    sourceSets {
        val wasmJsMain by getting {
            dependencies {
                implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
                // Local npm packages: the wasm-bindgen output and the thin JS bridge.
                implementation(npm("protocol-wasm", "file:$protocolPkgDir"))
                implementation(npm("vade-bridge", "file:$bridgePkgDir"))
                implementation(npm("ws", "8.18.3"))
            }
        }
    }
}
